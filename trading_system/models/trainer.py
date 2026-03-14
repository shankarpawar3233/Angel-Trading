"""
XGBoost classifier training with time-based split and isotonic calibration.
"""

import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import accuracy_score, precision_score, classification_report
from sklearn.preprocessing import LabelEncoder
import xgboost as xgb

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings


FEATURE_COLUMNS = [
    "log_return", "candle_range", "candle_body",
    "ema21", "ema_slope", "price_to_ema",
    "atr14", "atr_ratio",
    "rsi14",
    "hour", "minute",
]


def get_feature_columns():
    return FEATURE_COLUMNS.copy()


def prepare_xy(df: pd.DataFrame):
    """Extract X and y from labeled DataFrame; encode labels."""
    cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    if not cols or "label" not in df.columns:
        return None, None, None
    X = df[cols].copy()
    X = X.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0)
    y = df["label"].copy()
    le = LabelEncoder()
    y_enc = le.fit_transform(y.astype(str))
    return X, y_enc, le


def time_based_split(X: pd.DataFrame, y: np.ndarray, train_ratio: float = None):
    """Split by time: first train_ratio for train, rest for test."""
    train_ratio = train_ratio or settings.TRAIN_SPLIT
    n = len(X)
    split_idx = int(n * train_ratio)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    return X_train, X_test, y_train, y_test


def train_model(labeled_df: pd.DataFrame, save_path: str = None):
    """
    Train XGBoost with isotonic calibration; save model and label encoder.
    Returns (model, label_encoder, metrics_dict).
    """
    save_path = save_path or settings.MODEL_PATH
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)

    X, y, le = prepare_xy(labeled_df)
    if X is None or len(X) < 50:
        return None, None, {}

    X_train, X_test, y_train, y_test = time_based_split(X, y)

    base = xgb.XGBClassifier(
        n_estimators=settings.N_ESTIMATORS,
        max_depth=settings.MAX_DEPTH,
        learning_rate=settings.LEARNING_RATE,
        objective="multi:softprob",
        num_class=len(le.classes_),
        random_state=42,
        eval_metric="mlogloss",
    )
    calibrated = CalibratedClassifierCV(base, method="isotonic", cv=3)
    calibrated.fit(X_train, y_train)

    y_pred = calibrated.predict(X_test)
    y_proba = calibrated.predict_proba(X_test)
    acc = accuracy_score(y_test, y_pred)
    prec_macro = precision_score(y_test, y_pred, average="macro", zero_division=0)
    prec_per_class = precision_score(y_test, y_pred, average=None, zero_division=0)

    max_proba = y_proba.max(axis=1)
    metrics = {
        "accuracy": acc,
        "precision_macro": prec_macro,
        "precision_per_class": prec_per_class,
        "classes": le.classes_.tolist(),
        "confidence_mean": float(max_proba.mean()),
        "confidence_std": float(max_proba.std()),
    }

    artifact = {
        "model": calibrated,
        "label_encoder": le,
        "feature_columns": FEATURE_COLUMNS,
    }
    joblib.dump(artifact, save_path)

    return calibrated, le, metrics


def load_trained_artifact(path: str = None):
    """Load dict with 'model', 'label_encoder', 'feature_columns'."""
    path = path or settings.MODEL_PATH
    if not Path(path).exists():
        return None
    return joblib.load(path)
