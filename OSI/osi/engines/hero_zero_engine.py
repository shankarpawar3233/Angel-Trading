from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from osi.core.models import EngineOutput, MarketTick
from osi.data.instrument_master import load_instruments
from osi.engines.base import BaseEngine
from osi.services.expiry_engine import ExpiryEngine

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

        nearest_expiry = self._nearest_expiry(chain, tick.symbol)
        total_call_volume = 0.0
        total_put_volume = 0.0
        # tick.option_chain shape: {expiry_ymd: {strike: {"CE": {...}, "PE": {...}}}}
        for expiry_key, by_strike in chain.items():
            if nearest_expiry and str(expiry_key) != nearest_expiry:
                continue
            if not isinstance(by_strike, dict):
                continue
            for row in by_strike.values():
                if not isinstance(row, dict):
                    continue
                ce = row.get("CE") or {}
                pe = row.get("PE") or {}
                total_call_volume += float(ce.get("volume", 0.0) or 0.0)
                total_put_volume += float(pe.get("volume", 0.0) or 0.0)

        if (total_call_volume + total_put_volume) <= 0:
            logger.debug(
                "[%s] NONE reason=no_near_expiry_volume expiry=%s",
                self.name,
                nearest_expiry,
            )
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_near_expiry_volume")

        sweep_up = c_high > p_high and c_close < p_high
        sweep_down = c_low < p_low and c_close > p_low
        if not sweep_up and not sweep_down:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="no_sweep_reversal")

        signal = "BUY_PE" if sweep_up else "BUY_CE"
        option_momentum = self._option_momentum_strength(tick, state, signal, nearest_expiry)
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
    def _nearest_expiry(chain: Dict, symbol: str) -> str:
        if not chain:
            return ""
        return ExpiryEngine().select_chain_expiry(symbol, list(chain.keys()), load_instruments())

    def _option_momentum_strength(
        self,
        tick: MarketTick,
        state: Dict,
        signal: str,
        nearest_expiry: Optional[str] = None,
    ) -> float:
        chain = tick.option_chain or {}
        ce_legs: List[float] = []
        pe_legs: List[float] = []
        for expiry_key, by_strike in chain.items():
            if nearest_expiry and str(expiry_key) != nearest_expiry:
                continue
            if not isinstance(by_strike, dict):
                continue
            for row in by_strike.values():
                if not isinstance(row, dict):
                    continue
                ce_ltp = (row.get("CE") or {}).get("ltp")
                pe_ltp = (row.get("PE") or {}).get("ltp")
                if ce_ltp is not None:
                    ce_legs.append(float(ce_ltp))
                if pe_ltp is not None:
                    pe_legs.append(float(pe_ltp))

        if not ce_legs or not pe_legs:
            return 0.0

        avg_ce = sum(ce_legs) / len(ce_legs)
        avg_pe = sum(pe_legs) / len(pe_legs)
        snap = state.setdefault("hero_zero_leg_avg_ltp", {})
        prev_ce = float(snap.get((tick.symbol, "CE")) or 0.0)
        prev_pe = float(snap.get((tick.symbol, "PE")) or 0.0)
        snap[(tick.symbol, "CE")] = avg_ce
        snap[(tick.symbol, "PE")] = avg_pe
        if prev_ce <= 0.0 or prev_pe <= 0.0:
            return 0.0

        delta_ce = avg_ce - prev_ce
        delta_pe = avg_pe - prev_pe

        if signal == "BUY_CE":
            # Up move: CE premium should rise; reward CE>PE divergence.
            directional = delta_ce - delta_pe
            baseline = max(1.0, prev_ce * 0.01)
        else:
            # Down move: PE premium should rise; reward PE>CE divergence.
            directional = delta_pe - delta_ce
            baseline = max(1.0, prev_pe * 0.01)

        if directional <= 0.0:
            return 0.0
        return self.clamp(directional / baseline)
