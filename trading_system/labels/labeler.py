"""
Forward-looking label generation: BUY / SELL / HOLD over next 3 candles (15 min).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings


def compute_future_return(close: pd.Series, horizon: int) -> pd.Series:
    """Return (close[t+horizon] / close[t]) - 1."""
    future = close.shift(-horizon)
    return (future / close - 1).astype(float)


def generate_labels(
    df: pd.DataFrame,
    close_col: str = "close",
    horizon: int = None,
    threshold: float = None,
) -> pd.DataFrame:
    """
    Add label column: BUY if future_return > threshold, SELL if < -threshold, else HOLD.
    """
    horizon = horizon or settings.FORWARD_HORIZON
    threshold = threshold or settings.MOVE_THRESHOLD
    out = df.copy()
    close = out[close_col]
    future_ret = compute_future_return(close, horizon)
    out["future_return"] = future_ret
    out["label"] = "HOLD"
    out.loc[future_ret > threshold, "label"] = "BUY"
    out.loc[future_ret < -threshold, "label"] = "SELL"
    # Drop rows with no future (last `horizon` rows)
    out = out.iloc[:-horizon] if horizon > 0 and len(out) > horizon else out
    out = out.dropna(subset=["future_return"])
    return out


def generate_and_save_labels(features_df: pd.DataFrame, save_path: str = None) -> pd.DataFrame:
    """Generate labels from features (must have 'close'), save to labeled_data.csv."""
    if "close" not in features_df.columns:
        return pd.DataFrame()
    labeled = generate_labels(features_df)
    if save_path is None:
        save_path = settings.LABELED_DATA_PATH
    if not labeled.empty and save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        labeled_copy = labeled.copy()
        if "timestamp" in labeled_copy.columns and hasattr(labeled_copy["timestamp"].iloc[0], "strftime"):
            labeled_copy["timestamp"] = labeled_copy["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        labeled_copy.to_csv(save_path, index=False)
    return labeled
