"""
Market regime detection.

Detects TREND_UP, TREND_DOWN, RANGE, VOLATILE using EMA9, EMA21, VWAP, ATR, RSI, volume.
Returns regime and confidence.
"""

from __future__ import annotations

from typing import Any, Dict, Literal

import numpy as np
import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)

RegimeType = Literal["TREND_UP", "TREND_DOWN", "RANGE", "VOLATILE"]


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=length, adjust=False).mean()
    avg_loss = loss.ewm(span=length, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def detect_market_regime(candles: pd.DataFrame) -> Dict[str, Any]:
    """
    Detect market regime from OHLCV candles.

    Uses: EMA9, EMA21, VWAP, ATR, RSI, volume.

    Returns:
        {
            "regime": "TREND_UP" | "TREND_DOWN" | "RANGE" | "VOLATILE",
            "confidence": 0-100,
            "indicators": {"ema9", "ema21", "vwap", "atr", "rsi", "volume_ratio"},
        }
    """
    if candles is None or candles.empty or len(candles) < 30:
        return {
            "regime": "RANGE",
            "confidence": 0,
            "indicators": {},
        }

    df = candles.copy()
    if "ts" in df.columns:
        df = df.sort_values("ts").reset_index(drop=True)
    n = len(df)
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema_9"] = _ema(close, 9)
    df["ema_21"] = _ema(close, 21)
    typical = (high + low + close) / 3.0
    cum_vol = df["volume"].cumsum() if "volume" in df.columns else pd.Series(1, index=df.index).cumsum()
    df["vwap"] = (typical * df["volume"]).cumsum() / cum_vol.replace(0, np.nan)
    df["vwap"] = df["vwap"].fillna(close.expanding().mean())
    df["atr"] = _atr(high, low, close, 14)
    df["rsi"] = _rsi(close, 14)

    row = df.iloc[-1]
    ema9 = row.get("ema_9")
    ema21 = row.get("ema_21")
    vwap = row.get("vwap")
    atr = row.get("atr")
    rsi = row.get("rsi")
    price = float(close.iloc[-1])

    if pd.isna(ema9) or pd.isna(ema21):
        return {"regime": "RANGE", "confidence": 0, "indicators": {}}

    atr_ratio = float(atr / price * 100) if atr and price else 0
    vol_ratio = 1.0
    if "volume" in df.columns and df["volume"].iloc[-5:].mean() > 0:
        vol_ratio = float(df["volume"].iloc[-1] / (df["volume"].iloc[-21:-1].mean() + 1e-9))

    # Trend: price vs EMAs and VWAP
    above_ema = price > ema9 and price > ema21
    below_ema = price < ema9 and price < ema21
    above_vwap = price > vwap if pd.notna(vwap) else False
    below_vwap = price < vwap if pd.notna(vwap) else False

    regime: RegimeType = "RANGE"
    confidence = 50

    if above_ema and above_vwap and (rsi is None or rsi < 75):
        regime = "TREND_UP"
        confidence = min(90, 55 + (float(price - ema21) / (ema21 + 1e-9) * 500))
    elif below_ema and below_vwap and (rsi is None or rsi > 25):
        regime = "TREND_DOWN"
        confidence = min(90, 55 + (float(ema21 - price) / (ema21 + 1e-9) * 500))
    elif atr_ratio > 1.5 and vol_ratio > 1.3:
        regime = "VOLATILE"
        confidence = min(85, 50 + int(atr_ratio * 10))
    else:
        regime = "RANGE"
        confidence = 55

    confidence = max(0, min(95, int(confidence)))

    logger.info("[REGIME] Market regime detected: %s (confidence %s)", regime, confidence)

    return {
        "regime": regime,
        "confidence": confidence,
        "indicators": {
            "ema9": float(ema9),
            "ema21": float(ema21),
            "vwap": float(vwap) if pd.notna(vwap) else None,
            "atr": float(atr),
            "rsi": float(rsi) if pd.notna(rsi) else None,
            "volume_ratio": vol_ratio,
        },
    }
