from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Tuple

from osi.core.config import settings
from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class IntrabarEngine(BaseEngine):
    name = "intrabar_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        live = tick.meta.get("live_candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        vwap = float(tick.meta.get("vwap") or 0.0)
        if not live or not prev or vwap <= 0:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        try:
            p_high = float(prev.get("high"))
            p_low = float(prev.get("low"))
            c_high = float(live.get("high"))
            c_low = float(live.get("low"))
            bucket_id = int(live.get("bucket_id"))
        except (TypeError, ValueError):
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        if bucket_id <= 0 or (c_high - c_low) < float(settings.intrabar_volatility_min_range):
            logger.debug("[%s] NONE reason=low_volatility_or_bad_bucket", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        momentum = self._momentum(tick, state)
        min_momentum = float(settings.intrabar_momentum_threshold)
        if abs(momentum) < min_momentum:
            logger.debug("[%s] NONE reason=low_momentum m=%.3f min=%.3f", self.name, momentum, min_momentum)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        bullish = tick.index_price > p_high and momentum > 0 and tick.index_price > vwap
        bearish = tick.index_price < p_low and momentum < 0 and tick.index_price < vwap
        if not bullish and not bearish:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        if not self._allow_trigger(tick.symbol, bucket_id, state):
            logger.debug("[%s] NONE reason=debounce_or_cooldown", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0)

        breakout_dist = max(
            (tick.index_price - p_high) if bullish else (p_low - tick.index_price),
            0.0,
        )
        live_range = max(1e-6, c_high - c_low)
        mom_score = abs(momentum) / max(min_momentum, 1e-6)
        breakout_score = breakout_dist / live_range
        strength = self.clamp(0.55 + 0.20 * breakout_score + 0.15 * mom_score)
        confidence = max(60.0, min(80.0, 55.0 + (strength * 30.0)))
        signal = "BUY_CE" if bullish else "BUY_PE"
        self._mark_triggered(tick.symbol, bucket_id, state)
        return EngineOutput(
            engine=self.name,
            signal=signal,
            strength=round(max(0.7, min(0.9, strength)), 3),
            confidence=round(confidence, 2),
        )

    def _momentum(self, tick: MarketTick, state: Dict) -> float:
        hist: Dict[str, Deque[Tuple[float, float]]] = state.setdefault("intrabar_price_hist", {})
        row = hist.setdefault(tick.symbol, deque(maxlen=240))
        now_ts = self._ts(tick.timestamp)
        row.append((now_ts, float(tick.index_price)))
        lookback = now_ts - float(settings.intrabar_momentum_seconds)
        px_old = row[0][1]
        for ts, px in row:
            if ts >= lookback:
                px_old = px
                break
        return float(tick.index_price) - float(px_old)

    @staticmethod
    def _ts(ts: datetime) -> float:
        if isinstance(ts, datetime):
            return ts.timestamp()
        return datetime.now(timezone.utc).timestamp()

    def _allow_trigger(self, symbol: str, bucket_id: int, state: Dict) -> bool:
        trigger_bucket = state.setdefault("intrabar_trigger_bucket", {})
        if trigger_bucket.get(symbol) == bucket_id:
            return False
        last_fire = state.setdefault("intrabar_last_fire_ts", {}).get(symbol, 0.0)
        now_ts = datetime.now(timezone.utc).timestamp()
        if (now_ts - float(last_fire)) < float(settings.intrabar_cooldown_seconds):
            return False
        return True

    def _mark_triggered(self, symbol: str, bucket_id: int, state: Dict) -> None:
        state.setdefault("intrabar_trigger_bucket", {})[symbol] = bucket_id
        state.setdefault("intrabar_last_fire_ts", {})[symbol] = datetime.now(timezone.utc).timestamp()
