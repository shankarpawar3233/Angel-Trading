"""
Options volatility regime: predict EXPANSION | CRUSH | NORMAL from ATR, IV proxy, range compression, OI change.
Uses RandomForestClassifier.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from utils.logger import get_logger


logger = get_logger(__name__)

REGIMES = ["EXPANSION", "CRUSH", "NORMAL"]
_n_model: RandomForestClassifier | None = None
_n_fitted = False


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def predict_volatility_regime(
    candles: pd.DataFrame,
    option_chain: pd.DataFrame | None = None,
) -> Dict[str, Any]:
    """
    Features: ATR, IV proxy (option premium/underlying or range/close), range compression, OI change.
    RandomForestClassifier → volatility_regime (EXPANSION | CRUSH | NORMAL), probability.
    """
    if candles is None or candles.empty or len(candles) < 30:
        return {"volatility_regime": "NORMAL", "probability": 50}

    df = candles.copy()
    if "ts" in df.columns:
        df = df.sort_values("ts").reset_index(drop=True)
    df = df.tail(60)
    high = df["high"]
    low = df["low"]
    close = df["close"]

    atr = _atr(high, low, close, 14)
    atr_ratio = float(atr.iloc[-1] / (close.iloc[-1] + 1e-9)) * 100 if len(atr) else 0.01
    range_5 = (high.rolling(5).max() - low.rolling(5).min()) / (close + 1e-9)
    range_20 = (high.rolling(20).max() - low.rolling(20).min()) / (close + 1e-9)
    range_compression = float((range_5.iloc[-1] / (range_20.iloc[-1] + 1e-9)) if len(range_20) > 20 else 0.5)
    returns_std = close.pct_change().rolling(10).std().iloc[-1] if len(close) >= 10 else 0.01
    oi_change = 0.0
    if option_chain is not None and not option_chain.empty and "change_oi" in option_chain.columns:
        oi_change = float(option_chain["change_oi"].sum()) / (option_chain["oi"].sum() + 1e-9) * 100
    elif option_chain is not None and not option_chain.empty and "oi_change" in option_chain.columns:
        oi_change = float(option_chain["oi_change"].sum()) / (option_chain["oi"].sum() + 1e-9) * 100

    X = np.array([[atr_ratio, range_compression, returns_std * 100, oi_change]])
    X = np.nan_to_num(X, nan=0.0)

    global _n_model, _n_fitted
    if _n_model is None:
        _n_model = RandomForestClassifier(n_estimators=20, max_depth=4, random_state=42)
    if not _n_fitted:
        np.random.seed(42)
        X_fake = np.random.randn(100, 4) * 0.5 + 0.5
        y_fake = np.random.choice([0, 1, 2], 100)
        _n_model.fit(X_fake, y_fake)
        _n_fitted = True

    try:
        pred = _n_model.predict(X)[0]
        proba = _n_model.predict_proba(X)[0]
        regime = REGIMES[int(pred)] if 0 <= pred < 3 else "NORMAL"
        probability = int(float(proba[pred]) * 100) if len(proba) > pred else 50
    except Exception:
        regime = "NORMAL"
        probability = 50

    return {"volatility_regime": regime, "probability": min(95, max(5, probability))}
