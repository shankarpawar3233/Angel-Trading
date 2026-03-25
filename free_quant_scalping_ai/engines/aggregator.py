from __future__ import annotations

from typing import Any, Dict, List, Tuple

from engines.config import env_float

# Priority order for tie-breaks (first wins when scores close): HeroZero > Scalping > Hold > sides
PRIORITY: List[str] = ["hero_zero", "scalping", "hold", "call_side", "put_side"]

DEFAULT_WEIGHTS = {
    "hero_zero": 3.0,
    "scalping": 2.0,
    "hold": 1.0,
    "call_side": 1.5,
    "put_side": 1.5,
}


def _weights() -> Dict[str, float]:
    out = dict(DEFAULT_WEIGHTS)
    for k in out:
        v = env_float(f"ENGINE_WEIGHT_{k.upper()}", out[k])
        out[k] = v
    return out


def aggregate(engine_outputs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Weighted CE vs PE scores; hero_zero weighted highest.
    2+ engines agreeing on the winning side increases confidence; CE vs PE conflict dampens it.
    """
    weights = _weights()
    ce_score = 0.0
    pe_score = 0.0
    ce_names: List[str] = []
    pe_names: List[str] = []

    for name in PRIORITY:
        o = engine_outputs.get(name) or {}
        sig = str(o.get("signal") or "NO_TRADE").upper()
        conf = max(0.0, min(100.0, float(o.get("confidence") or 0.0)))
        w = float(weights.get(name, 1.0))
        contrib = w * (conf / 100.0)
        if sig == "BUY_CE":
            ce_score += contrib
            ce_names.append(name)
        elif sig == "BUY_PE":
            pe_score += contrib
            pe_names.append(name)

    margin = env_float("AGGREGATE_MARGIN", 1.08)
    conflict = bool(ce_names and pe_names)
    winner: str
    if ce_score > pe_score * margin:
        winner = "BUY_CE"
        agree = len(ce_names)
        agree_names = ce_names
        win_score = ce_score
        lose_score = pe_score
    elif pe_score > ce_score * margin:
        winner = "BUY_PE"
        agree = len(pe_names)
        agree_names = pe_names
        win_score = pe_score
        lose_score = ce_score
    else:
        winner = "NO_TRADE"
        agree = 0
        agree_names = []
        win_score = max(ce_score, pe_score)
        lose_score = min(ce_score, pe_score)

    # Confidence: base from winning score, boost if 2+ agree, cut if conflict
    base_conf = min(95.0, 40.0 + win_score * 22.0)
    if agree >= 2:
        base_conf = min(95.0, base_conf + env_float("AGGREGATE_AGREE_BONUS", 8.0))
    if conflict and winner != "NO_TRADE":
        base_conf = max(35.0, base_conf - env_float("AGGREGATE_CONFLICT_PENALTY", 12.0))

    total_dir = len(ce_names) + len(pe_names)
    agreement_ratio = (agree / total_dir) if total_dir > 0 else 0.0

    # Priority tie-break: if scores almost equal, prefer higher-priority engine's signal
    if winner == "NO_TRADE" and win_score > 0 and abs(ce_score - pe_score) / max(1e-6, max(ce_score, pe_score)) < 0.12:
        for name in PRIORITY:
            o = engine_outputs.get(name) or {}
            sig = str(o.get("signal") or "NO_TRADE").upper()
            if sig in ("BUY_CE", "BUY_PE"):
                winner = sig
                base_conf = min(90.0, float(o.get("confidence") or 50.0))
                agree_names = [name]
                agreement_ratio = 1.0 / max(1, total_dir)
                break

    reason_parts = [
        f"ce={ce_score:.2f}",
        f"pe={pe_score:.2f}",
        f"winner={winner}",
    ]
    if conflict:
        reason_parts.append("conflict")

    return {
        "signal": winner,
        "confidence": round(base_conf, 2),
        "reason": "|".join(reason_parts),
        "metadata": {
            "ce_score": round(ce_score, 4),
            "pe_score": round(pe_score, 4),
            "ce_engines": ce_names,
            "pe_engines": pe_names,
            "agreement_engines": agree_names,
            "agreement_ratio": round(agreement_ratio, 4),
            "conflict": conflict,
        },
    }
