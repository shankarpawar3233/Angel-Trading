from __future__ import annotations

"""
Daily self-training pipeline for the options direction model.

Steps:
1) Load candles from SQLite
2) Generate features
3) Create BUY_CE / BUY_PE / NO_TRADE labels based on future price movement
4) Train RandomForest + XGBoost ensemble
5) Persist to models/model.pkl
"""

import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd

from config.settings import settings, DB_PATH
from data.data_storage import load_candles
from features.feature_engineering import engineer_all_features
from models.ensemble_model import EnsembleModel
from utils.logger import get_logger


logger = get_logger(__name__)

MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "model.pkl"


def _build_labeled_dataset(symbol: str, timeframe: str = "1m") -> Tuple[np.ndarray, np.ndarray, list[str]]:
    df = load_candles(symbol, timeframe)
    if df.empty or len(df) < settings.sequence_length + 20:
        logger.warning("[TRAINING] Not enough data for %s %s", symbol, timeframe)
        return np.empty((0, 0)), np.empty((0,)), []

    feats = engineer_all_features(df)
    if feats.empty:
        logger.warning("[TRAINING] No engineered features for %s %s", symbol, timeframe)
        return np.empty((0, 0)), np.empty((0,)), []

    # Align closes with engineered features index
    closes = feats["close"].values
    horizon = 10
    threshold = 20.0

    # Future move: close_{t+10} - close_t
    fut = np.roll(closes, -horizon) - closes
    labels = np.full_like(closes, fill_value=2, dtype=int)  # default NO_TRADE -> 2

    labels[fut >= threshold] = 0  # BUY_CE
    labels[fut <= -threshold] = 1  # BUY_PE

    # Drop last `horizon` rows with invalid future
    valid_len = len(closes) - horizon
    feats = feats.iloc[:valid_len].copy()
    labels = labels[:valid_len]

    feature_names = [
        c
        for c in feats.columns
        if c
        not in {"ts", "open", "high", "low", "close", "volume", "support", "resistance"}
    ]
    X = feats[feature_names].values.astype("float32")
    y = labels.astype("int64")

    logger.info("[TRAINING] Built dataset for %s %s: X=%s y=%s", symbol, timeframe, X.shape, y.shape)
    return X, y, feature_names


def train_models() -> None:
    """
    Train ensemble models daily on most recent candles and persist to disk.
    """
    try:
        all_X = []
        all_y = []
        feature_names: list[str] | None = None

        for symbol in settings.indices:
            X, y, feats = _build_labeled_dataset(symbol, "1m")
            if X.size == 0:
                continue
            all_X.append(X)
            all_y.append(y)
            feature_names = feats  # assume same features for both indices

        if not all_X or feature_names is None:
            logger.warning("[TRAINING] No data available to train models.")
            return

        X_all = np.concatenate(all_X, axis=0)
        y_all = np.concatenate(all_y, axis=0)

        logger.info("[TRAINING] Training ensemble on %s samples", X_all.shape[0])
        ensemble = EnsembleModel(input_dim=len(feature_names))
        ensemble.fit(X_all, y_all)

        meta = {
            "feature_names": feature_names,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "class_map": {0: "BUY_CE", 1: "BUY_PE", 2: "NO_TRADE"},
        }
        payload = {
            "ensemble": ensemble,
            "meta": meta,
        }

        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(payload, f)

        logger.info("[TRAINING] Model retrained successfully at %s -> %s", meta["trained_at"], MODEL_PATH)
    except Exception as exc:
        logger.exception("[TRAINING] Model training failed: %s", exc)

