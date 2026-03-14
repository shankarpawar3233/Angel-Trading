from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Literal, Any

import pickle
import numpy as np
import torch

from config.settings import settings
from models.deep_market_model import DeepMarketPredictor
from models.ensemble_model import EnsembleModel


Label = Literal["BUY_CE", "BUY_PE", "NO_TRADE"]


LABEL_INDEX: Dict[Label, int] = {"BUY_CE": 0, "BUY_PE": 1, "NO_TRADE": 2}
INDEX_LABEL: Dict[int, Label] = {v: k for k, v in LABEL_INDEX.items()}


@dataclass
class CombinedPredictor:
    feature_names: list[str]

    # Shared loaded model across instances
    _loaded: bool = False
    _ensemble: EnsembleModel | None = None
    _meta: Dict[str, Any] | None = None

    model_version: str | None = None

    def __post_init__(self):
        self.input_dim = len(self.feature_names)
        self.deep = DeepMarketPredictor(self.input_dim)
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """
        Lazily load trained ensemble model from disk, if available.
        Fallback to an untrained placeholder ensemble.
        """
        if CombinedPredictor._loaded:
            self.model_version = (
                CombinedPredictor._meta.get("trained_at") if CombinedPredictor._meta else None
            )
            return

        model_path = Path(__file__).resolve().parents[0] / "model.pkl"
        if model_path.exists():
            try:
                with open(model_path, "rb") as f:
                    payload = pickle.load(f)
                ens_payload = payload.get("ensemble")
                meta = payload.get("meta", {})
                if isinstance(ens_payload, EnsembleModel):
                    CombinedPredictor._ensemble = ens_payload
                    CombinedPredictor._meta = meta
                    CombinedPredictor._loaded = True
                    self.model_version = meta.get("trained_at")
                    return
            except Exception:
                # Fall back to neutral ensemble
                pass

        # Fallback: neutral ensemble
        CombinedPredictor._ensemble = EnsembleModel(self.input_dim)
        CombinedPredictor._meta = {"trained_at": None}
        CombinedPredictor._loaded = True
        self.model_version = None

    def _prepare_sequence_tensor(self, df_window) -> torch.Tensor:
        X = df_window[self.feature_names].values.astype("float32")
        X = np.nan_to_num(X, nan=0.0)
        X = torch.from_numpy(X).unsqueeze(0)  # (1, seq_len, input_dim)
        return X

    def predict(self, df_features) -> dict:
        if len(df_features) < settings.sequence_length:
            return {
                "label": "NO_TRADE",
                "probs": {"BUY_CE": 0.33, "BUY_PE": 0.33, "NO_TRADE": 0.34},
            }

        window = df_features.iloc[-settings.sequence_length :]
        x_seq = self._prepare_sequence_tensor(window)

        deep_probs = self.deep.predict_proba(x_seq).cpu().numpy()[0]

        X_last = window[self.feature_names].values[-1:].astype("float32")
        X_last = np.nan_to_num(X_last, nan=0.0)
        # Use trained ensemble if available (already loaded), else neutral probabilities.
        ens = CombinedPredictor._ensemble or EnsembleModel(self.input_dim)
        ens_probs = ens.predict_proba(X_last)[0]

        probs = 0.6 * deep_probs + 0.4 * ens_probs
        best_idx = int(probs.argmax())
        label = INDEX_LABEL[best_idx]

        return {
            "label": label,
            "probs": {
                "BUY_CE": float(probs[LABEL_INDEX["BUY_CE"]]),
                "BUY_PE": float(probs[LABEL_INDEX["BUY_PE"]]),
                "NO_TRADE": float(probs[LABEL_INDEX["NO_TRADE"]]),
            },
        }

