"""
Charts: price with BUY/SELL markers, feature importance, probability distribution,
signal timeline, signal density heatmap.
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False


def _ensure_dir():
    Path(settings.VISUALS_DIR).mkdir(parents=True, exist_ok=True)


def chart_price_with_signals(
    raw_df: pd.DataFrame,
    results_df: pd.DataFrame,
    save_path: str = None,
) -> None:
    """Price chart with BUY/SELL markers."""
    _ensure_dir()
    save_path = save_path or str(Path(settings.VISUALS_DIR) / "price_with_signals.png")
    if raw_df is None or raw_df.empty or "timestamp" not in raw_df.columns or "close" not in raw_df.columns:
        return
    if results_df is None:
        results_df = pd.DataFrame()
    ts = pd.to_datetime(raw_df["timestamp"])
    close = raw_df["close"].values
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(ts, close, color="gray", alpha=0.8, label="Close")
    if not results_df.empty and "timestamp" in results_df.columns:
        res_ts = pd.to_datetime(results_df["timestamp"])
        valid = results_df[results_df.get("valid_signal", pd.Series(True))] if "valid_signal" in results_df.columns else results_df
        buy = valid[valid["signal"] == "BUY"]
        sell = valid[valid["signal"] == "SELL"]
        for _, r in buy.iterrows():
            t = r["timestamp"]
            p = r.get("spot_price", close[-1])
            ax.scatter([t], [p], color="green", s=80, marker="^", zorder=5)
        for _, r in sell.iterrows():
            t = r["timestamp"]
            p = r.get("spot_price", close[-1])
            ax.scatter([t], [p], color="red", s=80, marker="v", zorder=5)
    ax.set_title("NIFTY 5m close with BUY/SELL signals")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def chart_feature_importance(artifact: dict, save_path: str = None) -> None:
    """Feature importance from XGBoost (if available)."""
    _ensure_dir()
    save_path = save_path or str(Path(settings.VISUALS_DIR) / "feature_importance.png")
    if not artifact or "model" not in artifact:
        return
    model = artifact["model"]
    try:
        if hasattr(model, "calibrated_classifiers_") and len(model.calibrated_classifiers_) > 0:
            base = getattr(model.calibrated_classifiers_[0], "estimator", model.calibrated_classifiers_[0])
        else:
            base = model
    except (IndexError, AttributeError):
        base = model
    if not hasattr(base, "feature_importances_"):
        return
    cols = artifact.get("feature_columns", [])
    if not cols:
        return
    imp = base.feature_importances_
    fig, ax = plt.subplots(figsize=(8, 6))
    idx = np.argsort(imp)[::-1][:min(15, len(imp))]
    ax.barh([cols[i] for i in idx], imp[idx], color="steelblue", alpha=0.8)
    ax.set_title("Feature importance (XGBoost)")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def chart_probability_distribution(results_df: pd.DataFrame, save_path: str = None) -> None:
    """Histogram of prediction confidence."""
    _ensure_dir()
    save_path = save_path or str(Path(settings.VISUALS_DIR) / "probability_distribution.png")
    if results_df is None or results_df.empty or "confidence" not in results_df.columns:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(results_df["confidence"], bins=30, color="teal", alpha=0.7, edgecolor="black")
    ax.axvline(settings.MIN_PROBABILITY, color="red", linestyle="--", label=f"Min prob ({settings.MIN_PROBABILITY})")
    ax.set_title("Prediction confidence distribution")
    ax.set_xlabel("Confidence")
    ax.legend()
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def chart_signal_timeline(results_df: pd.DataFrame, save_path: str = None) -> None:
    """Signals over time (BUY/SELL/HOLD)."""
    _ensure_dir()
    save_path = save_path or str(Path(settings.VISUALS_DIR) / "signal_timeline.png")
    if results_df is None or results_df.empty or "timestamp" not in results_df.columns or "signal" not in results_df.columns:
        return
    df = results_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")
    map_val = {"BUY": 1, "SELL": -1, "HOLD": 0}
    df["signal_val"] = df["signal"].map(map_val).fillna(0)
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.scatter(df["timestamp"], df["signal_val"], c=df["signal_val"].map({-1: "red", 0: "gray", 1: "green"}), alpha=0.7, s=10)
    ax.axhline(0, color="black", linewidth=0.5)
    ax.set_yticks([-1, 0, 1])
    ax.set_yticklabels(["SELL", "HOLD", "BUY"])
    ax.set_title("Signal timeline")
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def chart_signal_heatmap(results_df: pd.DataFrame, save_path: str = None) -> None:
    """Signal density by hour and minute."""
    _ensure_dir()
    save_path = save_path or str(Path(settings.VISUALS_DIR) / "signal_density_heatmap.png")
    if results_df is None or results_df.empty or "timestamp" not in results_df.columns or "signal" not in results_df.columns:
        return
    df = results_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["hour"] = df["timestamp"].dt.hour
    df["minute"] = df["timestamp"].dt.minute
    df["is_trade"] = (df["signal"] != "HOLD").astype(int)
    pivot = df.pivot_table(values="is_trade", index="hour", columns="minute", aggfunc="mean", fill_value=0)
    if pivot.empty:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    im = ax.imshow(pivot.values, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(0, pivot.shape[1], 3))
    ax.set_xticklabels(pivot.columns[::3])
    ax.set_yticks(range(pivot.shape[0]))
    ax.set_yticklabels(pivot.index)
    ax.set_xlabel("Minute")
    ax.set_ylabel("Hour")
    ax.set_title("Signal density (BUY/SELL) by time")
    plt.colorbar(im, ax=ax, label="Fraction")
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def generate_all_charts(
    raw_df: pd.DataFrame,
    results_df: pd.DataFrame,
    artifact: dict,
) -> None:
    """Generate all 5 charts and save to storage/visuals/."""
    if results_df is None:
        results_df = pd.DataFrame()
    chart_price_with_signals(raw_df, results_df)
    chart_feature_importance(artifact)
    chart_probability_distribution(results_df)
    chart_signal_timeline(results_df)
    chart_signal_heatmap(results_df)
