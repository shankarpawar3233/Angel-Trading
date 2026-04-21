from __future__ import annotations

import logging
from typing import Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class HeroZeroEngine(BaseEngine):
    name = "hero_zero_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        chain = tick.option_chain or {}
        total_call_volume = 0.0
        total_put_volume = 0.0
        for row in chain.values():
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            total_call_volume += float(ce.get("volume", 0.0) or 0.0)
            total_put_volume += float(pe.get("volume", 0.0) or 0.0)

        if (total_call_volume + total_put_volume) <= 0:
            logger.debug("[%s] NONE reason=no_option_volume", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)
        total = total_call_volume + total_put_volume + 1e-6
        put_skew = (total_put_volume - total_call_volume) / total
        if abs(put_skew) < 0.03:
            logger.debug("[%s] NONE reason=neutral_skew skew=%.4f", self.name, put_skew)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)
        strength = self.clamp(abs(put_skew) * 2.0)
        signal = "BUY_PE" if put_skew > 0 else "BUY_CE" if put_skew < 0 else "NONE"
        return EngineOutput(engine=self.name, signal=signal, strength=round(strength, 3))

