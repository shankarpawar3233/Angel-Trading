from __future__ import annotations

from typing import Any, Dict

from utils.logger import get_logger


logger = get_logger(__name__)


def _strategy_by_regime(regime: str) -> str:
    """Strategy switching: which strategy to emphasize by regime (Section 14)."""
    r = (regime or "").upper()
    if r == "TREND_UP":
        return "breakout_call"
    if r == "TREND_DOWN":
        return "breakout_put"
    if r == "RANGE":
        return "mean_reversion_scalping"
    if r == "VOLATILE":
        return "hero_zero"
    return "scalping"


def merge_signals(
    ml_decision: Dict,
    rl_decision: Dict,
    scalping: Dict,
    hero_zero: Dict,
    institutional: Dict,
    gamma: Dict,
    expiry: Dict,
    sweep: Dict,
    model_version: str | None = None,
    oi_analysis: Dict | None = None,
    gamma_exposure: Dict | None = None,
    max_pain: Dict | None = None,
    expiry_bias: Dict | None = None,
    liquidity_map: Dict | None = None,
    regime: Dict | None = None,
    stop_hunt: Dict | None = None,
) -> Dict:
    """
    Final signal fusion: combines ML, RL, scalping, hero-zero, institutional flow, gamma,
    gamma exposure (GEX), max pain, expiry bias, expiry, OI analysis, and liquidity sweep.

    We also apply a simple consensus filter so that scalping trades are only kept
    when they broadly agree with sweep / ML, otherwise they are suppressed.
    """
    scalping_sig = scalping.get("signal")

    # Consensus gating for scalping trades
    if scalping_sig is not None:
        trade = (scalping_sig.get("trade") or "").upper()
        sweep_dir = (sweep or {}).get("direction")
        ml_label = ml_decision.get("label")
        ml_probs = ml_decision.get("probs", {}) or {}
        ml_conf_for_trade = float(ml_probs.get(trade, 0.0))

        consensus = True

        # If a sweep is detected and points the other way, treat as conflict
        if sweep.get("detected") and sweep_dir and sweep_dir != trade:
            consensus = False

        # If ML strongly disagrees with the scalping trade, also treat as conflict
        if ml_label and ml_label != trade and ml_conf_for_trade < 0.65:
            consensus = False

        # If there is no supporting evidence from either ML (confident) or sweep,
        # be conservative and suppress the scalping trade.
        has_strong_ml_support = ml_label == trade and ml_conf_for_trade >= 0.65
        has_sweep_support = sweep.get("detected") and sweep_dir == trade
        if not has_strong_ml_support and not has_sweep_support:
            consensus = False

        if not consensus:
            scalping_sig = None

    out = {
        "ml": ml_decision,
        "rl": rl_decision,
        "scalping": scalping_sig,
        "hero_zero": hero_zero.get("candidates", []),
        "institutional_flow": institutional.get("summary"),
        "gamma": gamma,
        "gamma_levels": gamma_exposure if gamma_exposure else gamma,
        "max_pain": max_pain.get("max_pain") if isinstance(max_pain, dict) else None,
        "expiry_bias": expiry_bias.get("expiry_bias") if isinstance(expiry_bias, dict) else None,
        "expiry_bias_range": expiry_bias.get("expected_range") if isinstance(expiry_bias, dict) else None,
        "expiry": expiry.get("prediction") if isinstance(expiry, dict) else None,
        "liquidity_sweep": sweep,
        "liquidity_map": liquidity_map,
        "regime": regime,
        "stop_hunt": stop_hunt,
        "model_version": model_version,
    }
    if oi_analysis:
        out["oi_analysis"] = oi_analysis
    return out


def build_final_signal(
    symbol: str,
    price: float,
    fused: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build final output signal per Section 19.

    Returns single object with symbol, price, regime, gamma_wall, max_pain,
    institutional_flow, hero_zero, trade (BUY_CE/BUY_PE/NO_TRADE), confidence.
    """
    regime_obj = fused.get("regime") or {}
    regime_str = regime_obj.get("regime", "RANGE")
    gamma_levels = fused.get("gamma_levels") or fused.get("gamma") or {}
    if isinstance(gamma_levels, dict):
        gamma_wall = gamma_levels.get("gamma_wall_call")
    else:
        gamma_wall = None
    max_pain_val = fused.get("max_pain")
    inst = fused.get("institutional_flow")
    inst_flow = "NEUTRAL"
    if isinstance(inst, dict) and inst.get("description"):
        d = str(inst.get("description", "")).upper()
        if "LONG BUILD" in d or "LONG_BUILDUP" in d:
            inst_flow = "LONG_BUILDUP"
        elif "SHORT BUILD" in d or "SHORT_BUILDUP" in d:
            inst_flow = "SHORT_BUILDUP"
        elif "SHORT COVER" in d:
            inst_flow = "SHORT_COVERING"
        elif "LONG UNWIND" in d:
            inst_flow = "LONG_UNWINDING"
    elif isinstance(inst, dict) and inst.get("flow_type"):
        inst_flow = str(inst.get("flow_type", "NEUTRAL"))

    hero_candidates = fused.get("hero_zero") or []
    hero_zero_obj = hero_candidates[0] if hero_candidates else None

    # Trade: from ML, scalping, or regime-based default
    ml = fused.get("ml") or {}
    trade = ml.get("label", "NO_TRADE")
    scalping_sig = fused.get("scalping")
    if scalping_sig and scalping_sig.get("trade"):
        trade = scalping_sig.get("trade")
    strategy = _strategy_by_regime(regime_str)
    if strategy == "hero_zero" and hero_zero_obj and trade == "NO_TRADE":
        trade = "BUY_CE" if (hero_zero_obj.get("type") == "CE") else "BUY_PE"
    elif strategy == "breakout_call" and trade == "NO_TRADE":
        trade = "BUY_CE"
    elif strategy == "breakout_put" and trade == "NO_TRADE":
        trade = "BUY_PE"

    probs = ml.get("probs") or {}
    confidence = int(
        float(probs.get(trade, 0.33)) * 100
        if trade in probs
        else (regime_obj.get("confidence") or 50)
    )
    confidence = max(0, min(99, confidence))

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "regime": regime_str,
        "gamma_wall": gamma_wall,
        "max_pain": max_pain_val,
        "institutional_flow": inst_flow,
        "hero_zero": hero_zero_obj,
        "trade": trade,
        "confidence": confidence,
    }

