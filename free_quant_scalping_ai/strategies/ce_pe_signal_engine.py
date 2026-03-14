from __future__ import annotations

from typing import Dict


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
) -> Dict:
    """
    Final signal fusion: combines ML, RL, scalping, hero-zero, institutional flow, gamma, expiry,
    OI analysis, and liquidity sweep info.

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
        "expiry": expiry.get("prediction") if isinstance(expiry, dict) else None,
        "liquidity_sweep": sweep,
        "model_version": model_version,
    }
    if oi_analysis:
        out["oi_analysis"] = oi_analysis
    return out

