from __future__ import annotations

import logging
from typing import Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class OptionChainEngine(BaseEngine):
    name = "option_chain_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        chain = tick.option_chain or {}
        call_oi_total = 0.0
        put_oi_total = 0.0
        for row in chain.values():
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            call_oi_total += float(ce.get("oi", 0.0) or 0.0)
            put_oi_total += float(pe.get("oi", 0.0) or 0.0)

        if (call_oi_total + put_oi_total) <= 0:
            logger.debug("[%s] NONE reason=no_oi", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)
        total = call_oi_total + put_oi_total + 1e-6
        imbalance = (put_oi_total - call_oi_total) / total
        if abs(imbalance) < 0.02:
            logger.debug("[%s] NONE reason=balanced_oi imbalance=%.4f", self.name, imbalance)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)
        strength = self.clamp(abs(imbalance) * 3)
        signal = "BUY_PE" if imbalance > 0 else "BUY_CE" if imbalance < 0 else "NONE"
        return EngineOutput(engine=self.name, signal=signal, strength=round(strength, 3))

