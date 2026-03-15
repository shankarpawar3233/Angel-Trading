"""
Hidden Markov Model for market regime prediction.

Uses features: returns, volatility, volume, VWAP distance.
Predicts regime probability (TREND_UP, TREND_DOWN, RANGE, VOLATILE).
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)

REGIMES = ["TREND_UP", "TREND_DOWN", "RANGE", "VOLATILE"]


def _extract_regime_features(candles: pd.DataFrame, lookback: int = 20) -> np.ndarray:
    """Extract returns, volatility, volume norm, VWAP distance for HMM."""
    if candles is None or candles.empty or len(candles) < lookback:
        return np.zeros((0, 4))

    df = candles.copy()
    if "ts" in df.columns:
        df = df.sort_values("ts").reset_index(drop=True)
    df = df.tail(lookback)

    returns = df["close"].pct_change().fillna(0).values
    vol = df["close"].rolling(5).std().fillna(0).values
    if "volume" in df.columns:
        vol_ratio = (df["volume"] / (df["volume"].rolling(10).mean() + 1e-9)).fillna(1).values
    else:
        vol_ratio = np.ones(len(df))
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum() if "volume" in df.columns else pd.Series(1, index=df.index).cumsum()
    vwap = (typical * df["volume"]).cumsum() / cum_vol.replace(0, np.nan)
    vwap = vwap.fillna(df["close"])
    vwap_dist = (df["close"].values - vwap.values) / (df["close"].values + 1e-9)

    X = np.column_stack([
        np.nan_to_num(returns, nan=0),
        np.nan_to_num(vol, nan=0),
        np.nan_to_num(vol_ratio, nan=1),
        np.nan_to_num(vwap_dist, nan=0),
    ])
    return X


def predict_regime_hmm(candles: pd.DataFrame, n_regimes: int = 4) -> Dict[str, Any]:
    """
    Predict regime probabilities using HMM (if hmmlearn available) or rule-based fallback.

    Returns:
        {
            "regime": "TREND_UP" | "TREND_DOWN" | "RANGE" | "VOLATILE",
            "probabilities": {"TREND_UP": 0.3, "TREND_DOWN": 0.2, "RANGE": 0.4, "VOLATILE": 0.1},
            "confidence": 0-100,
        }
    """
    try:
        from hmmlearn import hmm
    except ImportError:
        return _regime_fallback(candles)

    X = _extract_regime_features(candles, lookback=60)
    if len(X) < 30:
        return _regime_fallback(candles)

    try:
        model = hmm.GaussianHMM(n_components=n_regimes, covariance_type="diag", n_iter=20, random_state=42)
        model.fit(X)
        probs = model.predict_proba(X)
        last_probs = probs[-1]
        regime_idx = int(np.argmax(last_probs))
        regime = REGIMES[regime_idx % len(REGIMES)]
        confidence = int(float(last_probs[regime_idx]) * 100)
        probabilities = {REGIMES[i]: float(last_probs[i]) for i in range(min(len(REGIMES), len(last_probs)))}
        return {
            "regime": regime,
            "probabilities": probabilities,
            "confidence": min(95, max(0, confidence)),
        }
    except Exception as e:
        logger.debug("[REGIME] HMM fit failed: %s", e)
        return _regime_fallback(candles)


def _regime_fallback(candles: pd.DataFrame) -> Dict[str, Any]:
    """Rule-based regime from recent returns and volatility."""
    if candles is None or candles.empty or len(candles) < 5:
        return {
            "regime": "RANGE",
            "probabilities": {r: 0.25 for r in REGIMES},
            "confidence": 0,
        }
    df = candles.tail(20)
    ret = df["close"].pct_change().dropna()
    mean_ret = ret.mean()
    vol = ret.std() or 1e-9
    if mean_ret > 0.001 and vol < mean_ret * 2:
        regime = "TREND_UP"
        probs = {"TREND_UP": 0.5, "TREND_DOWN": 0.1, "RANGE": 0.3, "VOLATILE": 0.1}
    elif mean_ret < -0.001 and vol < abs(mean_ret) * 2:
        regime = "TREND_DOWN"
        probs = {"TREND_UP": 0.1, "TREND_DOWN": 0.5, "RANGE": 0.3, "VOLATILE": 0.1}
    elif vol > ret.abs().mean() * 2:
        regime = "VOLATILE"
        probs = {"TREND_UP": 0.15, "TREND_DOWN": 0.15, "RANGE": 0.2, "VOLATILE": 0.5}
    else:
        regime = "RANGE"
        probs = {"TREND_UP": 0.2, "TREND_DOWN": 0.2, "RANGE": 0.5, "VOLATILE": 0.1}
    return {
        "regime": regime,
        "probabilities": probs,
        "confidence": 60,
    }
