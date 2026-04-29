from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict

from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class HeroZeroEngine(BaseEngine):
    name = "zero_hero_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        candle = tick.meta.get("candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        if not candle or not prev:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_candle_context")
        try:
            c_high = float(candle.get("high"))
            c_low = float(candle.get("low"))
            c_close = float(candle.get("close"))
            p_high = float(prev.get("high"))
            p_low = float(prev.get("low"))
        except (TypeError, ValueError):
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="bad_candle_values")

        chain = tick.option_chain or {}
        if not chain:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_option_chain")

        nearest_expiry = self._nearest_expiry(chain)
        total_call_volume = 0.0
        total_put_volume = 0.0
        for row in chain.values():
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            if nearest_expiry:
                if str(ce.get("expiry") or "") != nearest_expiry and str(pe.get("expiry") or "") != nearest_expiry:
                    continue
            total_call_volume += float(ce.get("volume", 0.0) or 0.0)
            total_put_volume += float(pe.get("volume", 0.0) or 0.0)

        if (total_call_volume + total_put_volume) <= 0:
            logger.debug("[%s] NONE reason=no_near_expiry_volume", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_near_expiry_volume")

        sweep_up = c_high > p_high and c_close < p_high
        sweep_down = c_low < p_low and c_close > p_low
        if not sweep_up and not sweep_down:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_sweep_reversal")

        signal = "BUY_PE" if sweep_up else "BUY_CE"
        option_momentum = self._option_momentum_strength(tick, state, signal)
        if option_momentum < 0.6:
            return EngineOutput(
                engine=self.name,
                signal="NONE",
                strength=0.0,
                confidence=0.0,
                reason="weak_option_momentum",
            )

        total = total_call_volume + total_put_volume + 1e-6
        skew = abs(total_put_volume - total_call_volume) / total
        strength = self.clamp(0.8 + (0.1 * min(1.0, skew + option_momentum * 0.5)))
        confidence = min(95.0, max(80.0, 80.0 + option_momentum * 15.0))
        return EngineOutput(
            engine=self.name,
            signal=signal,
            strength=round(strength, 3),
            confidence=round(confidence, 2),
            reason="liquidity_sweep_reversal",
        )

    @staticmethod
    def _nearest_expiry(chain: Dict) -> str:
        expiries = set()
        for row in chain.values():
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            if ce.get("expiry"):
                expiries.add(str(ce["expiry"]))
            if pe.get("expiry"):
                expiries.add(str(pe["expiry"]))
        if not expiries:
            return ""

        def _key(val: str):
            try:
                return datetime.strptime(val, "%d%b%Y")
            except Exception:
                return datetime.max

        return min(expiries, key=_key)

    def _option_momentum_strength(self, tick: MarketTick, state: Dict, signal: str) -> float:
        leg = "CE" if signal == "BUY_CE" else "PE"
        legs = []
        for row in (tick.option_chain or {}).values():
            info = (row.get(leg) or {})
            ltp = info.get("ltp")
            if ltp is not None:
                legs.append(float(ltp))
        if not legs:
            return 0.0
        avg_ltp = sum(legs) / len(legs)
        snap = state.setdefault("hero_zero_leg_avg_ltp", {})
        prev = snap.get((tick.symbol, leg))
        snap[(tick.symbol, leg)] = avg_ltp
        if prev is None or prev <= 0:
            return 0.0
        delta = avg_ltp - float(prev)
        if signal == "BUY_PE":
            delta = max(0.0, delta)
        else:
            delta = max(0.0, delta)
        return self.clamp(delta / max(1.0, float(prev) * 0.01))

