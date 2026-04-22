from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Dict

from osi.core.config import settings
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

    def evaluate_intrabar(
        self,
        tick: MarketTick,
        state: Dict,
        live_candle: Dict | None,
        prev_candle: Dict | None,
    ) -> EngineOutput:
        regime = str(tick.meta.get("regime") or "").upper()
        if regime == "SIDEWAYS":
            logger.debug("[%s] NONE reason=sideways_regime_intrabar", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)
        if not live_candle or not prev_candle:
            logger.debug("[%s] NONE reason=missing_live_or_prev_candle", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        c_open = float(live_candle.get("open") or 0.0)
        c_close = float(live_candle.get("close") or 0.0)
        c_high = float(live_candle.get("high") or 0.0)
        c_low = float(live_candle.get("low") or 0.0)
        p_high = float(prev_candle.get("high") or 0.0)
        p_low = float(prev_candle.get("low") or 0.0)
        bucket_id = int(live_candle.get("bucket_id") or 0)
        if min(c_open, c_close, c_high, c_low, p_high, p_low) <= 0 or bucket_id <= 0:
            logger.debug("[%s] NONE reason=invalid_intrabar_inputs", self.name)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        body = abs(c_close - c_open)
        rng = max(1e-6, c_high - c_low)
        body_ratio = body / rng
        if body_ratio <= 0.5:
            logger.debug("[%s] NONE reason=body_ratio_lt_0.5 ratio=%.3f", self.name, body_ratio)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(body_ratio), 3))

        momentum = self._momentum_points(tick, state, int(settings.scalping_intrabar_momentum_sec))
        mode = str(settings.scalping_entry_mode or "confirmed").strip().lower()
        min_mom = float(settings.scalping_intrabar_min_momentum)
        if mode == "aggressive":
            min_mom *= 0.7
        if abs(momentum) < min_mom:
            logger.debug("[%s] NONE reason=low_momentum momentum=%.3f min=%.3f", self.name, momentum, min_mom)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(abs(momentum) / max(min_mom, 1e-6)), 3))

        vol_spike = self._volume_spike_ratio(tick, state)
        min_spike = float(settings.scalping_volume_spike_ratio)
        if mode == "aggressive":
            min_spike *= 0.9
        if vol_spike < min_spike:
            logger.debug("[%s] NONE reason=no_volume_spike ratio=%.3f min=%.3f", self.name, vol_spike, min_spike)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(vol_spike / max(min_spike, 1e-6)), 3))

        breakout_up = c_close > p_high
        breakout_dn = c_close < p_low
        if not breakout_up and not breakout_dn:
            logger.debug("[%s] NONE reason=no_intrabar_breakout close=%.2f p_high=%.2f p_low=%.2f", self.name, c_close, p_high, p_low)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(self.clamp(body_ratio), 3))

        side = "BUY_CE" if breakout_up else "BUY_PE"
        valid = self._breakout_validity(
            symbol=tick.symbol,
            side=side,
            bucket_id=bucket_id,
            price=tick.index_price,
            breakout_level=p_high if breakout_up else p_low,
            state=state,
            mode=mode,
        )
        if not valid:
            logger.debug("[%s] NONE reason=fake_breakout_or_no_retest mode=%s", self.name, mode)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        if not self._option_ltp_confirmation(tick, state, side):
            logger.debug("[%s] NONE reason=option_ltp_not_confirmed side=%s", self.name, side)
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0)

        breakout_dist = max((c_close - p_high) if breakout_up else (p_low - c_close), 0.0)
        breakout_score = breakout_dist / rng
        mom_score = abs(momentum) / max(min_mom, 1e-6)
        vol_score = vol_spike / max(min_spike, 1e-6)
        strength = self.clamp(0.45 * body_ratio + 0.35 * breakout_score + 0.10 * mom_score + 0.10 * vol_score)
        if strength < 0.6:
            logger.debug("[%s] NONE reason=weak_intrabar_strength strength=%.3f", self.name, strength)
            return EngineOutput(engine=self.name, signal="NONE", strength=round(strength, 3))
        return EngineOutput(engine=self.name, signal=side, strength=round(strength, 3))

    def _momentum_points(self, tick: MarketTick, state: Dict, sec_window: int) -> float:
        from datetime import datetime, timezone

        hist = state.setdefault("scalp_tick_hist", {}).setdefault(tick.symbol, deque(maxlen=200))
        now_ts = float(tick.timestamp.timestamp() if isinstance(tick.timestamp, datetime) else datetime.now(timezone.utc).timestamp())
        hist.append((now_ts, float(tick.index_price)))
        cutoff = now_ts - max(1, sec_window)
        baseline = hist[0][1]
        for ts, px in hist:
            if ts >= cutoff:
                baseline = px
                break
        return float(tick.index_price) - float(baseline)

    def _volume_spike_ratio(self, tick: MarketTick, state: Dict) -> float:
        total_vol = 0.0
        for row in (tick.option_chain or {}).values():
            ce = row.get("CE", {}) if isinstance(row, dict) else {}
            pe = row.get("PE", {}) if isinstance(row, dict) else {}
            total_vol += float(ce.get("volume", 0.0) or 0.0) + float(pe.get("volume", 0.0) or 0.0)
        vol_hist = state.setdefault("scalp_volume_hist", {}).setdefault(tick.symbol, deque(maxlen=60))
        avg = (sum(vol_hist) / len(vol_hist)) if vol_hist else 0.0
        vol_hist.append(total_vol)
        if avg <= 0:
            return 1.0 if total_vol > 0 else 0.0
        return total_vol / avg

    def _breakout_validity(
        self,
        *,
        symbol: str,
        side: str,
        bucket_id: int,
        price: float,
        breakout_level: float,
        state: Dict,
        mode: str,
    ) -> bool:
        breakout_store = state.setdefault("scalp_breakout_state", {})
        key = f"{symbol}:{bucket_id}"
        b = breakout_store.get(key) or {
            "side": side,
            "bucket_id": bucket_id,
            "level": breakout_level,
            "retested": False,
            "failed": False,
        }
        tol = float(settings.scalping_retest_tolerance_points)
        if side == "BUY_CE":
            if price < (breakout_level - tol):
                b["failed"] = True
            if price <= (breakout_level + tol):
                b["retested"] = True
        else:
            if price > (breakout_level + tol):
                b["failed"] = True
            if price >= (breakout_level - tol):
                b["retested"] = True
        breakout_store[key] = b
        if b["failed"]:
            return False
        if mode == "aggressive":
            return True
        return bool(b["retested"])

    def _option_ltp_confirmation(self, tick: MarketTick, state: Dict, side: str) -> bool:
        chain = tick.option_chain or {}
        strikes = []
        for k in chain.keys():
            try:
                strikes.append(float(k))
            except (TypeError, ValueError):
                continue
        if not strikes:
            return False
        atm = min(strikes, key=lambda s: abs(s - float(tick.index_price)))
        row = chain.get(str(int(atm))) or chain.get(str(atm)) or {}
        ce_ltp = float(((row.get("CE") or {}).get("ltp") or 0.0))
        pe_ltp = float(((row.get("PE") or {}).get("ltp") or 0.0))
        ltp_hist = state.setdefault("scalp_option_ltp_hist", {}).setdefault(
            tick.symbol, {"ce": deque(maxlen=8), "pe": deque(maxlen=8)}
        )
        ltp_hist["ce"].append(ce_ltp)
        ltp_hist["pe"].append(pe_ltp)
        ce_prev = ltp_hist["ce"][-2] if len(ltp_hist["ce"]) >= 2 else ce_ltp
        pe_prev = ltp_hist["pe"][-2] if len(ltp_hist["pe"]) >= 2 else pe_ltp
        if side == "BUY_CE":
            return ce_ltp > ce_prev and ce_ltp >= pe_ltp * 0.9
        return pe_ltp > pe_prev and pe_ltp >= ce_ltp * 0.9

