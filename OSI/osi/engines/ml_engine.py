from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, List

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class MLEngine(BaseEngine):
    name = "ml_engine"

    def __init__(self, model_path: str = "storage/models/osi_rf_model.joblib") -> None:
        self.model_path = Path(model_path)
        self.model = None
        if self.model_path.exists():
            self.model = joblib.load(self.model_path)

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        if self.model is None:
            logger.debug("[%s] NONE reason=model_not_loaded", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.2)

        features = np.array([self._build_features(tick)])
        proba = self.model.predict_proba(features)[0]
        labels = list(self.model.classes_)
        best_idx = int(np.argmax(proba))
        label = str(labels[best_idx])
        strength = self.clamp(float(proba[best_idx]))
        if label not in ("BUY_CE", "BUY_PE"):
            logger.debug("[%s] NONE reason=class=%s", self.name, label)
            label = "NONE"
        return EngineOutput(engine=self.name, signal=label, strength=round(strength, 3))

    def train(self, rows: Iterable[Dict]) -> None:
        x: List[List[float]] = []
        y: List[str] = []
        for row in rows:
            x.append(
                [
                    float(row.get("momentum", 0.0)),
                    float(row.get("pcr", 1.0)),
                    float(row.get("oi_imbalance", 0.0)),
                    float(row.get("vol_imbalance", 0.0)),
                    float(row.get("price_vs_vwap", 0.0)),
                ]
            )
            y.append(str(row.get("label", "NONE")))

        if len(x) < 25:
            logger.warning("ML training skipped: not enough rows (%s)", len(x))
            return

        model = RandomForestClassifier(n_estimators=200, random_state=42, max_depth=8)
        model.fit(np.array(x), np.array(y))
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, self.model_path)
        self.model = model
        logger.info("ML engine trained with %s rows", len(x))

    @staticmethod
    def _build_features(tick: MarketTick) -> List[float]:
        ce_oi = 0.0
        pe_oi = 0.0
        ce_vol = 0.0
        pe_vol = 0.0
        for row in tick.option_chain.values():
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            ce_oi += float(ce.get("oi", 0.0) or 0.0)
            pe_oi += float(pe.get("oi", 0.0) or 0.0)
            ce_vol += float(ce.get("volume", 0.0) or 0.0)
            pe_vol += float(pe.get("volume", 0.0) or 0.0)

        total_oi = ce_oi + pe_oi + 1e-6
        total_vol = ce_vol + pe_vol + 1e-6
        pcr = pe_oi / (ce_oi + 1e-6)
        oi_imbalance = (pe_oi - ce_oi) / total_oi
        vol_imbalance = (pe_vol - ce_vol) / total_vol
        momentum = float(tick.meta.get("momentum", 0.0))
        price_vs_vwap = float(tick.meta.get("price_vs_vwap", 0.0))
        return [momentum, pcr, oi_imbalance, vol_imbalance, price_vs_vwap]

