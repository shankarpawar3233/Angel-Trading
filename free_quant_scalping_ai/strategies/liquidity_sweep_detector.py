from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class LiquiditySweepSignal:
    type: str
    direction: str
    confidence: float


def _detect_sweep(candles_1m: pd.DataFrame) -> Optional[LiquiditySweepSignal]:
    """
    Detect stop hunts / liquidity sweeps on 1m candles.

    High sweep:
        - High breaks above rolling 20-candle high
        - Long upper wick (high - max(open, close)) > body * k
        - Close back inside range (closes near mid or below)

    Low sweep:
        - Low breaks below rolling 20-candle low
        - Long lower wick (min(open, close) - low) > body * k
        - Close back inside range (closes near mid or above)
    """
    if candles_1m.empty or len(candles_1m) < 25:
        return None

    df = candles_1m.copy()
    df["rolling_high"] = df["high"].rolling(20).max()
    df["rolling_low"] = df["low"].rolling(20).min()
    df["vol_ma"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-21:-1]  # previous 20 candles

    if prev.empty or last["vol_ma"] == 0:
        return None

    o = last["open"]
    h = last["high"]
    l = last["low"]
    c = last["close"]
    body = abs(c - o)
    body = body if body > 0 else 1e-6

    vol_spike = last["volume"] > 3.0 * last["vol_ma"]

    # High sweep -> BUY_PE
    broke_high = h > prev["high"].max()
    upper_wick = h - max(o, c)
    move_pct_high = (h - prev["high"].max()) / max(prev["high"].max(), 1e-6)
    # close back inside: close <= mid of swept range
    close_reject_high = c <= (h + prev["high"].max()) / 2.0
    strong_high_rejection = (
        broke_high
        and upper_wick > 2.0 * body
        and move_pct_high >= 0.0015  # ~0.15%
        and close_reject_high
    )

    if strong_high_rejection and vol_spike:
        logger.info("[SWEEP] High liquidity sweep detected -> BUY_PE")
        return LiquiditySweepSignal(type="liquidity_sweep", direction="BUY_PE", confidence=80.0)

    # Low sweep -> BUY_CE
    broke_low = l < prev["low"].min()
    lower_wick = min(o, c) - l
    move_pct_low = (prev["low"].min() - l) / max(prev["low"].min(), 1e-6)
    close_reject_low = c >= (l + prev["low"].min()) / 2.0
    strong_low_rejection = (
        broke_low
        and lower_wick > 2.0 * body
        and move_pct_low >= 0.0015  # ~0.15%
        and close_reject_low
    )

    if strong_low_rejection and vol_spike:
        logger.info("[SWEEP] Low liquidity sweep detected -> BUY_CE")
        return LiquiditySweepSignal(type="liquidity_sweep", direction="BUY_CE", confidence=80.0)

    return None


def detect_liquidity_sweep(candles_1m: pd.DataFrame) -> Dict:
    sig = _detect_sweep(candles_1m)
    if not sig:
        return {
            "detected": False,
            "direction": None,
            "confidence": 0.0,
        }
    return {
        "detected": True,
        "direction": sig.direction,
        "confidence": sig.confidence,
    }

