"""
Prediction engine: load model, predict, apply signal gates (time, probability, volatility, trend).
"""

import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings
from config.calendar import is_allowed_prediction_time
from models.trainer import load_trained_artifact, FEATURE_COLUMNS


def _time_gate(dt: datetime) -> bool:
    """Allowed window 09:20–15:15, exclude 12:00–13:30."""
    return is_allowed_prediction_time(dt)


def _probability_gate(prob: float) -> bool:
    return prob >= settings.MIN_PROBABILITY


def _volatility_gate(atr_ratio: float) -> bool:
    if np.isnan(atr_ratio) or atr_ratio <= 0:
        return True
    return atr_ratio < settings.MAX_VOLATILITY_RATIO


def _trend_gate(ema_slope: float) -> bool:
    if np.isnan(ema_slope):
        return False
    return ema_slope > settings.MIN_EMA_SLOPE


def run_gates(row: pd.Series, dt: datetime, prob: float) -> bool:
    """True if signal passes all gates."""
    if not _time_gate(dt):
        return False
    if not _probability_gate(prob):
        return False
    atr_ratio = row.get("atr_ratio", np.nan)
    if not _volatility_gate(atr_ratio):
        return False
    ema_slope = row.get("ema_slope", np.nan)
    if not _trend_gate(ema_slope):
        return False
    return True


def predict_one(features_row: pd.DataFrame, artifact: dict) -> tuple:
    """
    Single row prediction. Returns (signal_str, confidence float, passed_gates bool).
    """
    if artifact is None or features_row.empty:
        return "HOLD", 0.0, False
    model = artifact["model"]
    le = artifact["label_encoder"]
    cols = [c for c in artifact.get("feature_columns", FEATURE_COLUMNS) if c in features_row.columns]
    if not cols:
        return "HOLD", 0.0, False
    X = features_row[cols].replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0)
    pred_enc = model.predict(X)[0]
    proba = model.predict_proba(X)[0]
    confidence = float(proba.max())
    signal = le.inverse_transform([pred_enc])[0]
    dt = features_row["timestamp"].iloc[0]
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    passed = run_gates(features_row.iloc[0], dt, confidence)
    return signal, confidence, passed


def predict_batch(features_df: pd.DataFrame, artifact: dict) -> pd.DataFrame:
    """
    Batch prediction with gates. Returns DataFrame with columns:
    timestamp, spot_price, signal, confidence, valid_signal, ...
    """
    if artifact is None or features_df.empty:
        return pd.DataFrame()
    model = artifact["model"]
    le = artifact["label_encoder"]
    cols = [c for c in artifact.get("feature_columns", FEATURE_COLUMNS) if c in features_df.columns]
    if not cols:
        return pd.DataFrame()
    X = features_df[cols].replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0)
    pred_enc = model.predict(X)
    proba = model.predict_proba(X)
    signals = le.inverse_transform(pred_enc)
    confidence = proba.max(axis=1)
    spot_price = features_df["close"].values if "close" in features_df.columns else np.nan
    timestamps = features_df["timestamp"].values

    valid = []
    for i in range(len(features_df)):
        row = features_df.iloc[i]
        dt = timestamps[i]
        if hasattr(dt, "to_pydatetime"):
            dt = dt.to_pydatetime()
        valid.append(run_gates(row, dt, confidence[i]))

    out = pd.DataFrame({
        "timestamp": timestamps,
        "spot_price": spot_price,
        "signal": signals,
        "confidence": confidence,
        "valid_signal": valid,
    })
    return out
