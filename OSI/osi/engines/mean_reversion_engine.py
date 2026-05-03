from __future__ import annotations

import logging
from collections import deque
from statistics import fmean, pstdev
from typing import Deque, Dict

from osi.core.market_phase import get_market_phase
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
        phase = str(tick.meta.get("market_phase") or get_market_phase(tick.timestamp))
        vwap = float(tick.meta.get("vwap") or 0.0)
        vwap_deviation = abs(float(tick.index_price) - vwap) / vwap if vwap > 0.0 else 0.0
        candle = tick.meta.get("candle") or {}
        try:
            c_open = float(candle.get("open") or tick.index_price)
            c_close = float(candle.get("close") or tick.index_price)
            c_high = float(candle.get("high") or tick.index_price)
            c_low = float(candle.get("low") or tick.index_price)
        except (TypeError, ValueError):
            c_open = c_close = c_high = c_low = float(tick.index_price)
        candle_range = max(1e-6, c_high - c_low)
        body_ratio = abs(c_close - c_open) / candle_range
        closes_near_high = (c_high - c_close) / candle_range <= 0.25
        closes_near_low = (c_close - c_low) / candle_range <= 0.25
        overextended_up = body_ratio >= 0.45 and closes_near_high
        overextended_down = body_ratio >= 0.45 and closes_near_low

        if phase == "MIDDAY":
            z_threshold = 1.05
            vwap_threshold = 0.004
            phase_mult = 1.0
        else:
            # Keep the engine independent outside midday, but require cleaner reversal evidence.
            z_threshold = 1.75
            vwap_threshold = 0.006
            phase_mult = 0.65

        base_strength = self.clamp(abs(z) / 3.0)
        reversal_ok = abs(z) >= z_threshold and (vwap <= 0.0 or vwap_deviation >= vwap_threshold)
        if z >= z_threshold and overextended_up and reversal_ok:
            strength = self.clamp(base_strength * phase_mult)
            confidence = min(84.0, max(35.0, 48.0 + strength * 45.0))
            return EngineOutput(
                engine=self.name,
                signal="BUY_PE",
                strength=round(strength, 3),
                confidence=round(confidence, 2),
                reason=f"phase={phase}|vwap_dev={vwap_deviation:.4f}|z={z:.2f}|overextended_up",
            )
        if z <= -z_threshold and overextended_down and reversal_ok:
            strength = self.clamp(base_strength * phase_mult)
            confidence = min(84.0, max(35.0, 48.0 + strength * 45.0))
            return EngineOutput(
                engine=self.name,
                signal="BUY_CE",
                strength=round(strength, 3),
                confidence=round(confidence, 2),
                reason=f"phase={phase}|vwap_dev={vwap_deviation:.4f}|z={z:.2f}|overextended_down",
            )
        logger.debug(
            "[%s] NONE reason=no_reversal phase=%s z=%.4f vwap_dev=%.4f body=%.3f",
            self.name,
            phase,
            z,
            vwap_deviation,
            body_ratio,
        )
        return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(abs(z) / 4), 3))

