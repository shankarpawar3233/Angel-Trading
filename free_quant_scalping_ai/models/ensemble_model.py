from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from xgboost import XGBClassifier


@dataclass
class EnsembleModel:
    input_dim: int

    def __post_init__(self):
        self.rf = RandomForestClassifier(
            n_estimators=50, max_depth=5, random_state=42
        )
        self.xgb = XGBClassifier(
            n_estimators=80,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            objective="multi:softprob",
            num_class=3,
        )
        self.is_fitted = False

    @classmethod
    def from_trained(cls, payload: Dict[str, Any]) -> "EnsembleModel":
        """
        Create an EnsembleModel from a pickled payload (used by daily_trainer).
        """
        obj = cls(input_dim=payload.get("input_dim", 0) or 0)
        obj.rf = payload["rf"]
        obj.xgb = payload["xgb"]
        obj.is_fitted = True
        return obj

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self.rf.fit(X, y)
        self.xgb.fit(X, y)
        self.is_fitted = True

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            # Return neutral probabilities (no strong view) before training.
            probs = np.tile(np.array([[0.33, 0.33, 0.34]]), (len(X), 1))
            return probs

        p_rf = self.rf.predict_proba(X)
        p_xgb = self.xgb.predict_proba(X)
        return 0.5 * p_rf + 0.5 * p_xgb

