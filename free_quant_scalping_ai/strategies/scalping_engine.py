from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd

from features.feature_engineering import engineer_all_features
from utils.helpers import ScalpingSignal, round_safe


def _pick_atm_strike(index_price: float, step: int = 50) -> float:
    return float(round(index_price / step) * step)


def generate_scalping_signal(
    symbol: str,
    candles_1m: pd.DataFrame,
    option_chain: pd.DataFrame,
    ml_decision: Dict,
    rl_decision: Dict,
    live_price_override: float | None = None,
) -> Dict:
    """
    Combine VWAP, EMA crossover, RSI momentum, volume spike, and basic breakout
    plus ML/RL directions to produce a scalping signal.
    If live_price_override is set, use it for displayed price and ATM strike (current market).
    """
    if candles_1m.empty:
        return {"signal": None}

    feats = engineer_all_features(candles_1m.copy())
    if feats.empty:
        return {"signal": None}

    last = feats.iloc[-1]
    # Use live price when provided so signal reflects current market, not stale DB candle
    index_price = float(live_price_override) if live_price_override is not None else float(last["close"])

    vwap_bull = last["close"] > last.get("vwap", last["close"])
    ema_bull = last.get("ema_9", last["close"]) > last.get("ema_21", last["close"])
    rsi_momentum = last.get("rsi", 50) > 55

    vol_ma = candles_1m["volume"].rolling(20).mean().iloc[-1]
    vol_spike = candles_1m["volume"].iloc[-1] > 1.5 * (vol_ma or 1)

    breakout_up = last.get("breakout_up", 0) == 1
    breakout_down = last.get("breakout_down", 0) == 1

    ml_label = ml_decision.get("label", "NO_TRADE")
    ml_probs = ml_decision.get("probs", {}) or {}
    ml_conf = float(ml_probs.get(ml_label, 0.0))
    # Only use ML when it is confident enough
    ml_bull = ml_label == "BUY_CE" and ml_conf >= 0.65
    ml_bear = ml_label == "BUY_PE" and ml_conf >= 0.65

    rl_action = rl_decision.get("action", "HOLD")
    rl_bull = rl_action == "BUY_CE"
    rl_bear = rl_action == "BUY_PE"

    bullish_score = sum(
        [
            vwap_bull,
            ema_bull,
            rsi_momentum,
            vol_spike,
            breakout_up,
            ml_bull,
            rl_bull,
        ]
    )
    bearish_score = sum(
        [
            not vwap_bull,
            not ema_bull,
            last.get("rsi", 50) < 45,
            vol_spike,
            breakout_down,
            ml_bear,
            rl_bear,
        ]
    )

    side: str = "NONE"
    score = 0
    if bullish_score >= max(bearish_score + 1, 3):
        side = "BUY_CE"
        score = bullish_score
    elif bearish_score >= max(bullish_score + 1, 3):
        side = "BUY_PE"
        score = bearish_score

    if side == "NONE":
        return {"signal": None}

    atm_strike = _pick_atm_strike(index_price)
    # Use simple reward multiples for target/SL
    entry = round_safe(option_chain["ltp"].median() if not option_chain.empty else 20.0)
    if entry is None or entry <= 0:
        entry = 20.0
    target = round_safe(entry * 1.4)
    stoploss = round_safe(entry * 0.8)

    confidence = min(95.0, 60.0 + 5.0 * score)

    signal = ScalpingSignal(
        symbol=symbol,
        index_price=index_price,
        trade=side,
        strike=f"{int(atm_strike)} {'CE' if side == 'BUY_CE' else 'PE'}",
        entry=entry,
        target=target,
        stoploss=stoploss,
        confidence=confidence,
        generated_at=datetime.now(timezone.utc),
    )

    return {"signal": asdict(signal)}

