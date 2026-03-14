"""
Feature engineering: price action, trend, volatility, momentum, time context.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings


def _safe_log(x: np.ndarray) -> np.ndarray:
    """Log return with inf/nan handling."""
    out = np.log(np.where(x > 0, x, np.nan))
    out = np.where(np.isfinite(out), out, np.nan)
    return out


def compute_log_return(close: pd.Series) -> pd.Series:
    """Log return: log(close / close_prev)."""
    return (np.log(close / close.shift(1))).astype(float)


def compute_candle_range(high: pd.Series, low: pd.Series) -> pd.Series:
    """Candle range: high - low."""
    return (high - low).astype(float)


def compute_candle_body(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Candle body: |close - open|."""
    return (close - open_).abs().astype(float)


def compute_ema(close: pd.Series, period: int) -> pd.Series:
    """EMA of close."""
    return close.ewm(span=period, adjust=False).mean()


def compute_ema_slope(ema: pd.Series) -> pd.Series:
    """Slope of EMA (pct change)."""
    return ema.pct_change().fillna(0)


def compute_price_to_ema(close: pd.Series, ema: pd.Series) -> pd.Series:
    """close / ema - 1."""
    return (close / ema - 1).replace([np.inf, -np.inf], np.nan)


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """ATR(period)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def compute_atr_ratio(atr: pd.Series, period: int = 20) -> pd.Series:
    """ATR / ATR_ma (volatility ratio)."""
    atr_ma = atr.rolling(period).mean()
    return (atr / atr_ma).replace([np.inf, -np.inf], np.nan)


def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    """RSI(period)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build feature set from OHLCV DataFrame (columns: timestamp, open, high, low, close, volume).
    """
    if df.empty or len(df) < max(settings.EMA_PERIOD, settings.ATR_PERIOD, settings.RSI_PERIOD) + 5:
        return pd.DataFrame()

    out = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]

    # Price action
    out["log_return"] = compute_log_return(close)
    out["candle_range"] = compute_candle_range(high, low)
    out["candle_body"] = compute_candle_body(open_, close)

    # Trend
    ema = compute_ema(close, settings.EMA_PERIOD)
    out["ema21"] = ema
    out["ema_slope"] = compute_ema_slope(ema)
    out["price_to_ema"] = compute_price_to_ema(close, ema)

    # Volatility
    atr = compute_atr(high, low, close, settings.ATR_PERIOD)
    out["atr14"] = atr
    out["atr_ratio"] = compute_atr_ratio(atr, 20)

    # Momentum
    out["rsi14"] = compute_rsi(close, settings.RSI_PERIOD)

    # Time context
    ts = pd.to_datetime(out["timestamp"])
    out["hour"] = ts.dt.hour
    out["minute"] = ts.dt.minute

    # Clean inf/nan
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out.ffill().bfill().fillna(0)

    return out


def generate_and_save_features(df: pd.DataFrame, save_path: str = None) -> pd.DataFrame:
    """Build features from OHLCV and optionally save to storage/processed/features.csv."""
    feat = build_features(df)
    if save_path is None:
        save_path = settings.PROCESSED_FEATURES_PATH
    if not feat.empty and save_path:
        from pathlib import Path
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        feat_copy = feat.copy()
        if hasattr(feat_copy["timestamp"].iloc[0], "strftime"):
            feat_copy["timestamp"] = feat_copy["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        feat_copy.to_csv(save_path, index=False)
    return feat
