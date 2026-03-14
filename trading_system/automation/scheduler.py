"""
Continuous prediction mode: run pipeline every 5 minutes.
"""

import sys
import time
from pathlib import Path
from datetime import datetime

import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings
from data.data_manager import load_raw_data, get_latest_candle_for_live, ensure_storage_dirs
from data.data_manager import save_raw_data
from features.engineer import build_features
from models.trainer import load_trained_artifact
from workflows.predictor import predict_one, run_gates
from workflows.options_suggestion import suggest_strike


def _append_candle(raw_path: str, new_candle: pd.DataFrame) -> pd.DataFrame:
    """Append latest candle to raw data and return full raw df."""
    existing = pd.read_csv(raw_path) if Path(raw_path).exists() else pd.DataFrame()
    if "timestamp" in existing.columns:
        existing["timestamp"] = pd.to_datetime(existing["timestamp"])
    if new_candle.empty:
        return existing
    new_candle = new_candle.copy()
    new_candle["timestamp"] = pd.to_datetime(new_candle["timestamp"])
    combined = pd.concat([existing, new_candle], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return combined


def run_live_cycle() -> dict:
    """
    One cycle: fetch latest candle, merge with raw, build features for last row,
    load model, predict, apply gates, suggest option. Return result dict.
    """
    ensure_storage_dirs()
    raw_path = settings.RAW_DATA_PATH
    latest = get_latest_candle_for_live()
    if latest.empty:
        # Use last row of existing raw data
        df_raw = load_raw_data()
        if df_raw.empty or len(df_raw) < 30:
            return {"status": "no_data", "timestamp": datetime.now().isoformat()}
        # Take last 100 rows to compute features
        df_raw = df_raw.tail(100).reset_index(drop=True)
    else:
        df_raw = load_raw_data()
        if not df_raw.empty:
            df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"])
        combined = _append_candle(raw_path, latest)
        if len(combined) < 30:
            return {"status": "insufficient_data", "timestamp": datetime.now().isoformat()}
        df_raw = combined.tail(100).reset_index(drop=True)
        save_raw_data(combined)

    features_df = build_features(df_raw)
    if features_df.empty or len(features_df) < 2:
        return {"status": "features_failed", "timestamp": datetime.now().isoformat()}

    last_row = features_df.iloc[-1:].copy()
    artifact = load_trained_artifact()
    if artifact is None:
        return {"status": "no_model", "timestamp": datetime.now().isoformat()}

    signal, confidence, passed = predict_one(last_row, artifact)
    spot = float(last_row["close"].iloc[0])
    suggested = suggest_strike(spot, signal, confidence) if signal in ("BUY", "SELL") else ""
    ts = last_row["timestamp"].iloc[0]
    if hasattr(ts, "strftime"):
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts_str = str(ts)

    result = {
        "timestamp": ts_str,
        "spot_price": spot,
        "signal": signal,
        "confidence": round(confidence, 4),
        "valid_signal": passed,
        "suggested_option": suggested,
        "status": "ok",
    }
    _append_live_result(result)
    return result


def _append_live_result(result: dict) -> None:
    """Append one live prediction to storage/processed/live_predictions.csv."""
    path = Path(settings.PROCESSED_FEATURES_PATH).parent / "live_predictions.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = pd.DataFrame([result])
    if path.exists():
        prev = pd.read_csv(path)
        row = pd.concat([prev, row], ignore_index=True)
    row.to_csv(path, index=False)


def run_live_loop(interval_minutes: int = None):
    """Run prediction every interval_minutes (default 5)."""
    interval_minutes = interval_minutes or settings.LIVE_INTERVAL_MINUTES
    interval_sec = interval_minutes * 60
    print(f"Live prediction mode: every {interval_minutes} min. Ctrl+C to stop.")
    while True:
        try:
            t0 = time.time()
            res = run_live_cycle()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {res}")
            elapsed = time.time() - t0
            sleep_sec = max(0, interval_sec - elapsed)
            time.sleep(sleep_sec)
        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(min(60, interval_sec))
