from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class ScalpingEngine(BaseEngine):
    name = "scalping_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        regime = str(tick.meta.get("regime") or "").upper()
        if regime == "SIDEWAYS":
            logger.debug("[%s] NONE reason=sideways_regime", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        candle = tick.meta.get("candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        if not candle or not prev:
            logger.debug("[%s] NONE reason=missing_candle_confirmation", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        c_open = float(candle.get("open") or 0.0)
        c_close = float(candle.get("close") or 0.0)
        c_high = float(candle.get("high") or 0.0)
        c_low = float(candle.get("low") or 0.0)
        p_high = float(prev.get("high") or 0.0)
        p_low = float(prev.get("low") or 0.0)
        if min(c_open, c_close, c_high, c_low, p_high, p_low) <= 0:
            logger.debug("[%s] NONE reason=invalid_candle_values", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        body = abs(c_close - c_open)
        rng = max(1e-6, c_high - c_low)
        body_ratio = body / rng
        if body_ratio < 0.55:
            logger.debug("[%s] NONE reason=weak_body ratio=%.3f", self.name, body_ratio)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(body_ratio), 3))

        breakout_up = c_close > p_high
        breakout_dn = c_close < p_low
        if not breakout_up and not breakout_dn:
            logger.debug("[%s] NONE reason=no_breakout close=%.2f prev_hi=%.2f prev_lo=%.2f", self.name, c_close, p_high, p_low)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(body_ratio), 3))

        price_hist: Dict[str, Deque[float]] = state.setdefault("scalp_price_hist", {})
        hist = price_hist.setdefault(tick.symbol, deque(maxlen=20))
        hist.append(tick.index_price)
        if len(hist) < 6:
            logger.debug("[%s] NONE reason=warmup len=%s", self.name, len(hist))
            return EngineOutput(engine=self.name, signal="NONE", strength=0.1)

        momentum = hist[-1] - hist[-5]
        volatility = max(hist) - min(hist) + 1e-6
        breakout_dist = max((c_close - p_high) if breakout_up else (p_low - c_close), 0.0)
        breakout_score = breakout_dist / rng
        strength = self.clamp(0.55 * body_ratio + 0.45 * breakout_score + 0.15 * (abs(momentum) / volatility))
        if strength < 0.6:
            logger.debug("[%s] NONE reason=weak_strength strength=%.3f", self.name, strength)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(strength, 3))
        if breakout_up:
            signal = "BUY_CE"
        elif breakout_dn:
            signal = "BUY_PE"
        else:
            logger.debug("[%s] NONE reason=flat_momentum", self.name)
            signal = "NONE"
        return EngineOutput(engine=self.name, signal=signal, strength=round(strength, 3))

