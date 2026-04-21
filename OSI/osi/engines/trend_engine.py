from __future__ import annotations

import logging
from collections import deque
from statistics import fmean
from typing import Deque, Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class TrendEngine(BaseEngine):
    name = "trend_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        store: Dict[str, Deque[float]] = state.setdefault("trend_hist", {})
        hist = store.setdefault(tick.symbol, deque(maxlen=80))
        hist.append(tick.index_price)

        if len(hist) < 30:
            logger.debug("[%s] NONE reason=warmup len=%s", self.name, len(hist))
            return EngineOutput(engine=self.name, signal="NONE", strength=0.1)

        prices = list(hist)
        fast_ma = fmean(prices[-8:])
        slow_ma = fmean(prices[-25:])
        delta = fast_ma - slow_ma
        scale = max(1e-6, abs(slow_ma) * 0.002)
        if abs(delta) < scale * 0.15:
            logger.debug("[%s] NONE reason=flat_trend delta=%.5f", self.name, delta)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.1)
        strength = self.clamp(abs(delta) / scale)
        signal = "BUY_CE" if delta > 0 else "BUY_PE" if delta < 0 else "NONE"
        return EngineOutput(engine=self.name, signal=signal, strength=round(strength, 3))

