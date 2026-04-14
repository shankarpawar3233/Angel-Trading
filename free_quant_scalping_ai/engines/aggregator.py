from __future__ import annotations

from typing import Any, Dict, List, Tuple

from engines.config import env_float

PRIORITY: List[str] = ["hero_zero", "scalping", "smc", "hold", "call_side", "put_side", "support_resistance"]

DEFAULT_WEIGHTS = {
    "hero_zero": 3.0,
    "scalping": 2.0,
    "smc": 0.75,
    "hold": 1.0,
    "call_side": 1.5,
    "put_side": 1.5,
    "support_resistance": 1.25,
}


def _base_weights() -> Dict[str, float]:
    out = dict(DEFAULT_WEIGHTS)
    for k in out:
        out[k] = env_float(f"ENGINE_WEIGHT_{k.upper()}", out[k])
    return out


def _weights_for_regime(regime: str) -> Dict[str, float]:
    w = _base_weights()
    r = (regime or "RANGING").upper()
    if r == "TRENDING":
        w["hold"] = 3.0
        w["scalping"] = 1.0
        w["hero_zero"] = 1.0
    elif r == "RANGING":
        w["scalping"] = 3.0
        w["hold"] = 1.0
    elif r == "EXPIRY_HIGH_GAMMA":
        w["hero_zero"] = 4.0
    return w


def normalize_confidence(engine: str, raw_confidence: float, regime: str) -> float:
    c = max(0.0, min(100.0, float(raw_confidence)))
    r = (regime or "").upper()
    if engine == "hold":
        return min(100.0, c * (1.12 if r == "TRENDING" else 0.95 if r == "RANGING" else 1.0))
    if engine == "hero_zero":
        return min(100.0, c * (1.18 if r == "EXPIRY_HIGH_GAMMA" else 0.85))
    if engine == "scalping":
        return c
    if engine == "smc":
        return min(100.0, c * (1.02 if r in ("TRENDING", "VOLATILE") else 0.98))
    if engine in ("call_side", "put_side"):
        return min(100.0, c * (1.04 if r == "TRENDING" else 1.0))
    return c


def _conflict_resolution(
    ce_score: float,
    pe_score: float,
    regime: str,
    support_resistance: Dict[str, Any] | None,
    engine_outputs: Dict[str, Dict[str, Any]],
) -> Tuple[str, float, str]:
    margin = env_float("AGGREGATE_MARGIN", 1.08)
    if ce_score > pe_score * margin:
        return "BUY_CE", ce_score, "score_margin"
    if pe_score > ce_score * margin:
        return "BUY_PE", pe_score, "score_margin"

    # Smart conflict case (e.g. scalping CE vs hold PE): use regime + S/R proximity
    r = (regime or "").upper()
    sc_sig = str((engine_outputs.get("scalping") or {}).get("signal") or "NO_TRADE")
    hd_sig = str((engine_outputs.get("hold") or {}).get("signal") or "NO_TRADE")
    sr = support_resistance or {}
    d_sup = float(sr.get("distance_support") or 0.0)
    d_res = float(sr.get("distance_resistance") or 0.0)

    if r == "TRENDING" and hd_sig in ("BUY_CE", "BUY_PE"):
        if d_res < 0 and hd_sig == "BUY_CE":
            return "BUY_CE", max(ce_score, pe_score), "trend_hold_breakout"
        if d_sup < 0 and hd_sig == "BUY_PE":
            return "BUY_PE", max(ce_score, pe_score), "trend_hold_breakdown"
        return hd_sig, max(ce_score, pe_score), "trend_hold_priority"

    if r in ("RANGING", "VOLATILE") and sc_sig in ("BUY_CE", "BUY_PE"):
        if d_sup > 0 and d_sup < d_res and sc_sig == "BUY_CE":
            return "BUY_CE", max(ce_score, pe_score), "range_support_reversal"
        if d_res > 0 and d_res < d_sup and sc_sig == "BUY_PE":
            return "BUY_PE", max(ce_score, pe_score), "range_resistance_reversal"
        return sc_sig, max(ce_score, pe_score), "range_scalping_priority"

    for name in PRIORITY:
        sig = str((engine_outputs.get(name) or {}).get("signal") or "NO_TRADE")
        if sig in ("BUY_CE", "BUY_PE"):
            return sig, max(ce_score, pe_score), f"priority_tiebreak_{name}"
    return "NO_TRADE", max(ce_score, pe_score), "no_directional_edge"


def aggregate(
    engine_outputs: Dict[str, Dict[str, Any]],
    *,
    regime: Dict[str, Any] | None = None,
    support_resistance: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    reg = regime or {"regime": "RANGING", "confidence": 0.0}
    regime_name = str(reg.get("regime") or "RANGING").upper()
    weights = _weights_for_regime(regime_name)

    ce_score = 0.0
    pe_score = 0.0
    ce_names: List[str] = []
    pe_names: List[str] = []
    normalized: Dict[str, float] = {}

    for name in PRIORITY:
        o = engine_outputs.get(name) or {}
        sig = str(o.get("signal") or "NO_TRADE").upper()
        nconf = normalize_confidence(name, float(o.get("confidence") or 0.0), regime_name)
        normalized[name] = nconf
        w = float(weights.get(name, 1.0))
        contrib = w * (nconf / 100.0)
        if sig == "BUY_CE":
            ce_score += contrib
            ce_names.append(name)
        elif sig == "BUY_PE":
            pe_score += contrib
            pe_names.append(name)

    conflict = bool(ce_names and pe_names)
    winner, win_score, resolve_reason = _conflict_resolution(ce_score, pe_score, regime_name, support_resistance, engine_outputs)
    agree_names = ce_names if winner == "BUY_CE" else pe_names if winner == "BUY_PE" else []
    agree = len(agree_names)
    total_dir = len(ce_names) + len(pe_names)
    agreement_ratio = (agree / total_dir) if total_dir > 0 else 0.0

    base_conf = min(95.0, 40.0 + win_score * 22.0)
    if agree >= 2:
        base_conf = min(95.0, base_conf + env_float("AGGREGATE_AGREE_BONUS", 8.0))
    if conflict and winner != "NO_TRADE":
        base_conf = max(35.0, base_conf - env_float("AGGREGATE_CONFLICT_PENALTY", 8.0))
    if regime_name == "VOLATILE":
        base_conf = max(25.0, base_conf - env_float("AGGREGATE_VOLATILE_PENALTY", 15.0))

    final_intent = "SCALP"
    for n in agree_names:
        intent = str((engine_outputs.get(n) or {}).get("intent") or "")
        if intent:
            final_intent = intent
            break

    reason_parts = [f"regime={regime_name}", f"ce={ce_score:.2f}", f"pe={pe_score:.2f}", f"winner={winner}", resolve_reason]
    if conflict:
        reason_parts.append("conflict")

    return {
        "signal": winner,
        "confidence": round(base_conf, 2),
        "intent": final_intent,
        "reason": "|".join(reason_parts),
        "metadata": {
            "regime": reg,
            "weights": weights,
            "normalized_confidence": normalized,
            "ce_score": round(ce_score, 4),
            "pe_score": round(pe_score, 4),
            "ce_engines": ce_names,
            "pe_engines": pe_names,
            "agreement_engines": agree_names,
            "agreement_ratio": round(agreement_ratio, 4),
            "conflict": conflict,
            "support_resistance": support_resistance or {},
        },
    }
