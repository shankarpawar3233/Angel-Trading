#!/usr/bin/env python3
"""
Master runner for NIFTY prediction system.
Run full pipeline: fetch data -> features -> labels -> train -> predict -> gates -> options -> charts.

Usage:
  python run_system.py           # one-shot full pipeline
  python run_system.py --live    # continuous prediction every 5 minutes
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure we run from trading_system directory so relative paths work
_SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(_SCRIPT_DIR)
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from config import settings
from data.data_manager import (
    ensure_storage_dirs,
    update_raw_dataset,
    load_raw_data,
    save_features,
    save_labeled_data,
    load_features,
    load_labeled_data,
)
from features.engineer import build_features, generate_and_save_features
from labels.labeler import generate_and_save_labels
from models.trainer import train_model, load_trained_artifact
from workflows.backtester import run_backtest, save_backtest_results
from visualization.charts import generate_all_charts
from automation.scheduler import run_live_loop


def run_pipeline():
    """Execute full pipeline: data -> features -> labels -> train -> predict -> options -> visuals."""
    print("=== NIFTY Prediction System — Full Pipeline ===\n")

    ensure_storage_dirs()

    # 1. Fetch historical data
    print("1. Fetching historical data...")
    raw_df = update_raw_dataset()
    if raw_df.empty:
        print("   No raw data. Exiting.")
        return
    print(f"   Loaded {len(raw_df)} rows -> storage/raw/nifty_5min.csv")

    # 2. Update dataset (already saved in update_raw_dataset)
    print("2. Dataset updated.")

    # 3. Generate features
    print("3. Generating features...")
    features_df = build_features(raw_df)
    if features_df.empty:
        print("   Feature generation failed. Exiting.")
        return
    save_features(features_df)
    print(f"   Saved -> {settings.PROCESSED_FEATURES_PATH}")

    # 4. Generate labels
    print("4. Generating labels...")
    labeled_df = generate_and_save_labels(features_df)
    if labeled_df.empty:
        print("   Label generation failed. Exiting.")
        return
    print(f"   Saved -> {settings.LABELED_DATA_PATH}")

    # 5. Train / update model
    print("5. Training model (XGBoost + isotonic calibration)...")
    model, le, metrics = train_model(labeled_df)
    if model is None:
        print("   Training failed. Exiting.")
        return
    print(f"   Model saved -> {settings.MODEL_PATH}")
    print(f"   Accuracy: {metrics.get('accuracy', 0):.4f}")
    print(f"   Precision (macro): {metrics.get('precision_macro', 0):.4f}")
    print(f"   Confidence mean: {metrics.get('confidence_mean', 0):.4f}")
    # Persist metrics for dashboard API
    import json
    metrics_path = Path(settings.PROCESSED_FEATURES_PATH).parent / "model_metrics.json"
    with open(metrics_path, "w") as f:
        json.dump({
            "accuracy": metrics.get("accuracy"),
            "precision_macro": metrics.get("precision_macro"),
            "confidence_mean": metrics.get("confidence_mean"),
            "confidence_std": metrics.get("confidence_std"),
        }, f, indent=2)

    # 6. Run predictions (backtest)
    print("6. Running predictions (backtest)...")
    results_df = run_backtest(features_df)
    if results_df.empty:
        print("   No predictions. Skipping save.")
    else:
        save_backtest_results(results_df)
        print(f"   Saved -> {settings.BACKTEST_RESULTS_PATH}")
        valid = results_df[results_df["valid_signal"]] if "valid_signal" in results_df.columns else results_df
        print(f"   Valid signals: {valid.shape[0]}")

    # 7. Prediction gates (already applied in predictor; results have valid_signal column)
    print("7. Signal gates applied (time, probability, volatility, trend).")

    # 8. Suggest option strikes (already in results_df from backtester)
    print("8. Option suggestions added to results.")

    # 9. Generate visual charts
    print("9. Generating charts...")
    artifact = load_trained_artifact()
    generate_all_charts(raw_df, results_df, artifact)
    print(f"   Charts -> {settings.VISUALS_DIR}/")

    # 10. Save results (already saved in step 6)
    print("10. Results saved.")
    print("\n=== Pipeline complete. ===")


def main():
    parser = argparse.ArgumentParser(description="NIFTY prediction system")
    parser.add_argument("--live", action="store_true", help="Continuous prediction every 5 minutes")
    args = parser.parse_args()

    if args.live:
        run_live_loop()
    else:
        run_pipeline()


if __name__ == "__main__":
    main()
