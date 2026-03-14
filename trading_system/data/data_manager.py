"""
Data manager: load/save raw and processed data; update dataset from Angel API.
"""

import sys
from pathlib import Path

import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings
from data.angel_client import fetch_historical_candles, fetch_latest_candle


def ensure_storage_dirs():
    """Create storage/raw, storage/processed, storage/models, storage/visuals."""
    for p in [
        settings.RAW_DATA_PATH,
        settings.PROCESSED_FEATURES_PATH,
        settings.LABELED_DATA_PATH,
        settings.MODEL_PATH,
        settings.BACKTEST_RESULTS_PATH,
        settings.VISUALS_DIR,
    ]:
        Path(p).parent.mkdir(parents=True, exist_ok=True)


def get_raw_path() -> Path:
    return Path(settings.RAW_DATA_PATH)


def load_raw_data() -> pd.DataFrame:
    """Load raw OHLCV from storage/raw/nifty_5min.csv."""
    p = get_raw_path()
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def save_raw_data(df: pd.DataFrame) -> None:
    """Save raw OHLCV to storage."""
    ensure_storage_dirs()
    df = df.copy()
    if "timestamp" in df.columns and hasattr(df["timestamp"].iloc[0], "strftime"):
        df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(get_raw_path(), index=False)


def update_raw_dataset() -> pd.DataFrame:
    """Fetch historical data from Angel (or mock), save to raw, return DataFrame."""
    ensure_storage_dirs()
    df = fetch_historical_candles(settings.LOOKBACK_DAYS)
    if df.empty:
        return load_raw_data()
    save_raw_data(df)
    return df


def load_features() -> pd.DataFrame:
    """Load features from storage/processed/features.csv."""
    p = Path(settings.PROCESSED_FEATURES_PATH)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def save_features(df: pd.DataFrame) -> None:
    """Save features to storage/processed/features.csv."""
    ensure_storage_dirs()
    df = df.copy()
    if "timestamp" in df.columns and hasattr(df["timestamp"].iloc[0], "strftime"):
        df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(settings.PROCESSED_FEATURES_PATH, index=False)


def load_labeled_data() -> pd.DataFrame:
    """Load labeled dataset from storage/processed/labeled_data.csv."""
    p = Path(settings.LABELED_DATA_PATH)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def save_labeled_data(df: pd.DataFrame) -> None:
    """Save labeled dataset."""
    ensure_storage_dirs()
    df = df.copy()
    if "timestamp" in df.columns and hasattr(df["timestamp"].iloc[0], "strftime"):
        df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(settings.LABELED_DATA_PATH, index=False)


def get_latest_candle_for_live() -> pd.DataFrame:
    """For live mode: fetch latest candle from API or return empty."""
    return fetch_latest_candle()
