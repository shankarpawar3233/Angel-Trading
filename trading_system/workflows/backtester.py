"""
Backtest workflow: run predictions on processed data and save results with option suggestions.
"""

import sys
from pathlib import Path

import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings
from models.trainer import load_trained_artifact
from workflows.predictor import predict_batch
from workflows.options_suggestion import add_option_suggestions


def run_backtest(features_df: pd.DataFrame, model_path: str = None) -> pd.DataFrame:
    """Run predictions on full feature set; add option suggestions; return results DataFrame."""
    artifact = load_trained_artifact(model_path or settings.MODEL_PATH)
    if artifact is None:
        return pd.DataFrame()
    results = predict_batch(features_df, artifact)
    if results.empty:
        return results
    results = add_option_suggestions(results)
    return results


def save_backtest_results(results: pd.DataFrame, path: str = None) -> None:
    path = path or settings.BACKTEST_RESULTS_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df = results.copy()
    if "timestamp" in df.columns and hasattr(df["timestamp"].iloc[0], "strftime"):
        df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(path, index=False)
