from __future__ import annotations

import logging
from collections import deque
from statistics import fmean, pstdev
from typing import Deque, Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class MeanReversionEngine(BaseEngine):
    name = "mean_reversion_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        store: Dict[str, Deque[float]] = state.setdefault("mr_hist", {})
        hist = store.setdefault(tick.symbol, deque(maxlen=60))
        hist.append(tick.index_price)
        if len(hist) < 20:
            logger.debug("[%s] NONE reason=warmup len=%s", self.name, len(hist))
            return EngineOutput(engine=self.name, signal="NONE", strength=0.1)

        prices = list(hist)
        mean_px = fmean(prices)
        sigma = pstdev(prices) + 1e-6
        z = (tick.index_price - mean_px) / sigma
        if z >= 1.2:
            return EngineOutput(engine=self.name, signal="BUY_PE", strength=round(self.clamp(abs(z) / 3), 3))
        if z <= -1.2:
            return EngineOutput(engine=self.name, signal="BUY_CE", strength=round(self.clamp(abs(z) / 3), 3))
        logger.debug("[%s] NONE reason=inside_band z=%.4f", self.name, z)
        return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(abs(z) / 4), 3))

