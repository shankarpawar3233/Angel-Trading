from __future__ import annotations

import numpy as np
import pandas as pd


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


def add_price_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["returns"] = df["close"].pct_change()
    df["log_returns"] = (df["close"] / df["close"].shift(1)).apply(
        lambda x: 0.0 if x <= 0 else float(np.log(x))
    )
    df["volatility"] = df["returns"].rolling(20).std()
    df["momentum"] = df["close"] - df["close"].shift(10)
    df["trend_strength"] = df["close"].rolling(20).apply(
        # Use iloc for position-based indexing (works for RangeIndex)
        lambda x: 0.0
        if float(x.std() or 0.0) == 0.0
        else float((x.iloc[-1] - x.iloc[0]) / (len(x) * x.std())),
        raw=False,
    )
    return df


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = _rsi(df["close"], 14)
    ema12 = _ema(df["close"], 12)
    ema26 = _ema(df["close"], 26)
    df["macd"] = ema12 - ema26
    df["macd_signal"] = _ema(df["macd"], 9)
    df["ema_9"] = _ema(df["close"], 9)
    df["ema_21"] = _ema(df["close"], 21)
    df["ema_50"] = _ema(df["close"], 50)
    df["atr"] = _atr(df["high"], df["low"], df["close"], 14)
    bb_mid = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["bb_low"] = bb_mid - 2 * bb_std
    df["bb_mid"] = bb_mid
    df["bb_high"] = bb_mid + 2 * bb_std
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    num = (typical_price * df["volume"]).cumsum()
    df["vwap"] = (num / cum_vol.replace(0, np.nan)).fillna(df["close"].expanding().mean())
    return df


def detect_support_resistance(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    df = df.copy()
    df["support"] = df["low"].rolling(window).min()
    df["resistance"] = df["high"].rolling(window).max()
    df["breakout_up"] = (df["close"] > df["resistance"].shift(1)).astype(int)
    df["breakout_down"] = (df["close"] < df["support"].shift(1)).astype(int)
    return df


def engineer_all_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = add_price_features(df)
    df = add_technical_indicators(df)
    df = add_vwap(df)
    df = detect_support_resistance(df)
    df = df.dropna().reset_index(drop=True)
    return df

