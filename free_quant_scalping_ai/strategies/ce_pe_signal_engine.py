from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)

MIN_VOLUME_FOR_STRIKE = 500
MIN_OI_FOR_STRIKE = 5000


def select_strike_from_chain(
    price: float,
    option_chain: pd.DataFrame,
    trade_type: str,
) -> Optional[Dict[str, Any]]:
    """
    Real strike selection from option chain.
    BUY_CE: ATM or ATM+1 strike. BUY_PE: ATM or ATM-1 strike.
    Reject strikes where volume < 500 or oi < 5000.
    Returns {"strike", "premium", "volume", "oi"} or None.
    """
    if option_chain is None or option_chain.empty or price <= 0:
        return None
    df = option_chain.copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return None
    df["volume"] = df["volume"].fillna(0)
    df["oi"] = df["oi"].fillna(0)
    df = df[(df["volume"] >= MIN_VOLUME_FOR_STRIKE) & (df["oi"] >= MIN_OI_FOR_STRIKE)]
    if df.empty:
        return None

    trade_upper = (trade_type or "").upper()
    opt_type = "CE" if trade_upper == "BUY_CE" else "PE" if trade_upper == "BUY_PE" else None
    if opt_type is None:
        return None

    sub = df[df["option_type"] == opt_type]
    if sub.empty:
        return None
    strikes_asc = sorted(sub["strike"].unique())
    if not strikes_asc:
        return None

    # ATM: strike closest to price
    atm_strike = min(strikes_asc, key=lambda s: abs(s - price))
    idx = strikes_asc.index(atm_strike) if atm_strike in strikes_asc else 0
    if trade_upper == "BUY_CE":
        # ATM or ATM+1 (next higher)
        candidate_strikes = [atm_strike]
        if idx + 1 < len(strikes_asc):
            candidate_strikes.append(strikes_asc[idx + 1])
    else:
        # BUY_PE: ATM or ATM-1 (next lower)
        candidate_strikes = [atm_strike]
        if idx - 1 >= 0:
            candidate_strikes.append(strikes_asc[idx - 1])

    for strike in candidate_strikes:
        row = sub[sub["strike"] == strike].iloc[0]
        vol = float(row.get("volume", 0))
        oi = float(row.get("oi", 0))
        if vol < MIN_VOLUME_FOR_STRIKE or oi < MIN_OI_FOR_STRIKE:
            continue
        premium = row.get("ltp")
        if premium is not None and not (pd.isna(premium)):
            premium = float(premium)
        else:
            premium = None
        return {
            "strike": float(strike),
            "premium": premium,
            "volume": vol,
            "oi": oi,
        }
    return None


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
    dealer_position: Dict | None = None,
    gamma_squeeze: Dict | None = None,
    smart_money: Dict | None = None,
    liquidity_trap: Dict | None = None,
    convergence: Dict | None = None,
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
        "dealer_position": dealer_position,
        "gamma_squeeze": gamma_squeeze,
        "smart_money": smart_money,
        "liquidity_trap": liquidity_trap,
        "convergence": convergence,
        "model_version": model_version,
    }
    if oi_analysis:
        out["oi_analysis"] = oi_analysis
    return out


def _compute_smart_confidence(
    trade: str,
    fused: Dict[str, Any],
    gamma_wall: Any,
    price: float,
    strike_info: Optional[Dict[str, Any]],
) -> int:
    """
    Multi-factor confidence: ml_prob*0.30 + flow_strength*0.20 + gamma_signal*0.20
    + liquidity_signal*0.15 + smart_money*0.15. Clamp 0-100.
    """
    ml = fused.get("ml") or {}
    probs = ml.get("probs") or {}
    ml_prob = float(probs.get(trade, 0.33))

    inst = fused.get("institutional_flow") or {}
    flow_strength = 0.0
    if isinstance(inst, dict):
        flow_strength = (inst.get("strength") or inst.get("confidence") or 0) / 100.0

    gs = fused.get("gamma_squeeze") or {}
    gamma_prob = (gs.get("probability") or 0) / 100.0
    squeeze_dir = (gs.get("squeeze_direction") or "NONE").upper()
    if trade == "BUY_CE" and squeeze_dir == "UP":
        gamma_signal = 0.5 + gamma_prob * 0.5
    elif trade == "BUY_PE" and squeeze_dir == "DOWN":
        gamma_signal = 0.5 + gamma_prob * 0.5
    else:
        gamma_signal = 0.5

    trap = fused.get("liquidity_trap") or {}
    trap_conf = (trap.get("confidence") or 0) / 100.0
    trap_type = (trap.get("trap_type") or "").upper()
    if trap.get("trap_detected"):
        if (trade == "BUY_CE" and trap_type == "BULL_TRAP") or (trade == "BUY_PE" and trap_type == "BEAR_TRAP"):
            liquidity_signal = 0.3
        else:
            liquidity_signal = 0.5 + trap_conf * 0.3
    else:
        liquidity_signal = 0.6

    sm = fused.get("smart_money") or {}
    sm_strength = (sm.get("strength") or 0) / 100.0
    sm_dir = (sm.get("smart_money_direction") or "NONE").upper()
    if (trade == "BUY_CE" and sm_dir == "CALL_ACCUMULATION") or (trade == "BUY_PE" and sm_dir == "PUT_ACCUMULATION"):
        smart_money_score = 0.4 + sm_strength * 0.6
    else:
        smart_money_score = 0.4

    confidence = (
        ml_prob * 0.30
        + flow_strength * 0.20
        + gamma_signal * 0.20
        + liquidity_signal * 0.15
        + smart_money_score * 0.15
    ) * 100
    return max(0, min(100, int(round(confidence))))


def build_final_signal(
    symbol: str,
    price: float,
    fused: Dict[str, Any],
    option_chain: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Build final output signal. Hard gate: NO_TRADE if no hero_zero, no scalping,
    no valid strike, or option_chain has < 10 strikes. Strike must come from
    select_strike_from_chain when option_chain is available.
    """
    regime_obj = fused.get("regime") or {}
    regime_str = regime_obj.get("regime", "RANGE")
    gamma_levels = fused.get("gamma_levels") or fused.get("gamma") or {}
    if isinstance(gamma_levels, dict):
        gamma_wall = gamma_levels.get("gamma_resistance") or gamma_levels.get("gamma_wall_call")
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
    elif isinstance(inst, dict) and (inst.get("flow_type") or inst.get("flow")):
        inst_flow = str(inst.get("flow_type") or inst.get("flow") or "NEUTRAL")

    hero_candidates = fused.get("hero_zero") or []
    hero_zero_obj = hero_candidates[0] if hero_candidates else None
    scalping_sig = fused.get("scalping")

    n_strikes = 0
    if option_chain is not None and not option_chain.empty and "strike" in option_chain.columns:
        n_strikes = int(option_chain["strike"].nunique())
    if n_strikes > 0:
        logger.info("[CHAIN] strikes loaded: %s", n_strikes)

    # Trade: from convergence (PCR + Gamma + MaxPain), ML, scalping, or regime-based default
    convergence = fused.get("convergence") or {}
    conv_bias = (convergence.get("bias") or "RANGE").upper()
    ml = fused.get("ml") or {}
    trade = ml.get("label", "NO_TRADE")
    # Apply convergence bias: BULLISH -> BUY_CE, BEARISH -> BUY_PE, RANGE -> NO_TRADE (when no other signal)
    if conv_bias == "BULLISH" and (trade == "NO_TRADE" or trade != "BUY_PE"):
        trade = "BUY_CE"
    elif conv_bias == "BEARISH" and (trade == "NO_TRADE" or trade != "BUY_CE"):
        trade = "BUY_PE"
    elif conv_bias == "RANGE" and trade != "NO_TRADE":
        # Reduce conviction when convergence says RANGE; keep ML/scalping if strong
        pass
    if scalping_sig and scalping_sig.get("trade"):
        trade = scalping_sig.get("trade")
    strategy = _strategy_by_regime(regime_str)
    if strategy == "hero_zero" and hero_zero_obj and trade == "NO_TRADE":
        trade = "BUY_CE" if (hero_zero_obj.get("type") == "CE") else "BUY_PE"
    elif strategy == "breakout_call" and trade == "NO_TRADE":
        trade = "BUY_CE"
    elif strategy == "breakout_put" and trade == "NO_TRADE":
        trade = "BUY_PE"

    # Real strike from chain when we have option_chain and a directional trade
    strike = None
    entry = None
    target = None
    stoploss = None
    strike_info = None
    if option_chain is not None and not option_chain.empty and trade in ("BUY_CE", "BUY_PE"):
        strike_info = select_strike_from_chain(price, option_chain, trade)
        if strike_info:
            strike = strike_info.get("strike")
            premium = strike_info.get("premium")
            if premium is not None:
                entry = premium
                target = round(premium * 1.4, 2)
                stoploss = round(premium * 0.8, 2)
            strike = f"{int(strike)} {'CE' if trade == 'BUY_CE' else 'PE'}"
        else:
            strike = None

    if strike is None and scalping_sig:
        entry = scalping_sig.get("entry")
        target = scalping_sig.get("target")
        stoploss = scalping_sig.get("stoploss")
        strike = scalping_sig.get("strike")
    if (strike is None or entry is None) and hero_zero_obj:
        entry = hero_zero_obj.get("entry")
        target = hero_zero_obj.get("target")
        stoploss = hero_zero_obj.get("stoploss")
        if strike is None:
            sk = hero_zero_obj.get("strike")
            ty = hero_zero_obj.get("type", "")
            strike = f"{int(sk)} {ty}" if sk is not None and ty else None

    # Hard trade gate: do NOT produce a trade when:
    if hero_zero_obj is None and scalping_sig is None:
        trade = "NO_TRADE"
        logger.info("[FINAL] signal generated: NO_TRADE (no hero_zero, no scalping)")
    if strike is None and trade != "NO_TRADE":
        trade = "NO_TRADE"
        logger.info("[FINAL] signal generated: NO_TRADE (no valid strike)")
    if n_strikes < 15 and trade != "NO_TRADE":
        trade = "NO_TRADE"
        logger.info("[FINAL] signal generated: NO_TRADE (option_chain < 15 strikes)")
    # Strict trade gating: do not generate trade if confidence < 55, option_chain < 15 strikes, or strike not selected
    if trade == "NO_TRADE":
        entry = target = stoploss = strike = None
        strike_info = None

    confidence = _compute_smart_confidence(trade, fused, gamma_wall, price, strike_info)
    if confidence < 55 and trade != "NO_TRADE":
        trade = "NO_TRADE"
        entry = target = stoploss = strike = None
        strike_info = None
        logger.info("[FINAL] signal generated: NO_TRADE (confidence < 55)")
    if trade != "NO_TRADE":
        logger.info("[FINAL] signal generated: %s strike=%s confidence=%s", trade, strike, confidence)

    def _round2(v):
        if v is None:
            return None
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    dealer_pos = fused.get("dealer_position")
    dealer_position = dealer_pos.get("dealer_position") if isinstance(dealer_pos, dict) else None
    gs = fused.get("gamma_squeeze")
    gamma_squeeze_val = gs.get("squeeze_direction") if isinstance(gs, dict) else None
    sm = fused.get("smart_money")
    smart_money_val = sm.get("smart_money_direction") if isinstance(sm, dict) else None
    oi = fused.get("oi_analysis") or {}
    pcr = oi.get("pcr") if isinstance(oi, dict) else None

    logger.info("[FINAL] signal generated: trade=%s confidence=%s", trade, confidence)

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "regime": regime_str,
        "gamma_wall": gamma_wall,
        "max_pain": max_pain_val,
        "pcr": pcr,
        "dealer_position": dealer_position,
        "gamma_squeeze": gamma_squeeze_val,
        "smart_money": smart_money_val,
        "institutional_flow": inst_flow,
        "hero_zero": hero_zero_obj,
        "trade": trade,
        "confidence": confidence,
        "entry": _round2(entry),
        "target": _round2(target),
        "stoploss": _round2(stoploss),
        "strike": strike,
    }

