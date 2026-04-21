from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class SMCEngine(BaseEngine):
    name = "smc_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        swing_store: Dict[str, Deque[float]] = state.setdefault("smc_swings", {})
        swings = swing_store.setdefault(tick.symbol, deque(maxlen=30))
        swings.append(tick.index_price)
        if len(swings) < 10:
            logger.debug("[%s] NONE reason=warmup len=%s", self.name, len(swings))
            return EngineOutput(engine=self.name, signal="NONE", strength=0.15)

        recent_high = max(list(swings)[-10:])
        recent_low = min(list(swings)[-10:])
        span = max(1e-6, recent_high - recent_low)
        premium = tick.index_price - (recent_low + span * 0.5)

        if tick.index_price >= recent_high * 0.999:
            return EngineOutput(engine=self.name, signal="BUY_CE", strength=0.75)
        if tick.index_price <= recent_low * 1.001:
            return EngineOutput(engine=self.name, signal="BUY_PE", strength=0.75)

        signal = "BUY_CE" if premium > 0 else "BUY_PE"
        strength = self.clamp(abs(premium) / span)
        return EngineOutput(engine=self.name, signal=signal, strength=round(strength, 3))

