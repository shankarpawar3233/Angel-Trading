from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Dict, Optional

from osi.core.config import settings
from osi.core.models import EngineOutput, MarketTick
from osi.engines.base import BaseEngine

logger = logging.getLogger(__name__)


def smart_breakout_engine(
    current_candle,
    previous_candle,
    vwap,
    avg_volume,
    intrabar_data=None,
):
    cur = current_candle or {}
    prev = previous_candle or {}
    intrabar = intrabar_data or {}

    try:
        c_open = float(cur.get("open") or 0.0)
        c_close = float(cur.get("close") or 0.0)
        c_high = float(cur.get("high") or 0.0)
        c_low = float(cur.get("low") or 0.0)
        c_vol = float(cur.get("volume") or 0.0)
        p_high = float(prev.get("high") or 0.0)
        p_low = float(prev.get("low") or 0.0)
        vwap_v = float(vwap or 0.0)
        avg_vol = float(avg_volume or 0.0)
    except (TypeError, ValueError):
        return {
            "engine": "smart_breakout_engine",
            "signal": "NONE",
            "strength": 0.05,
            "confidence": 10,
            "reason": "none",
        }

    candle_range = max(0.0, c_high - c_low)
    if candle_range < float(settings.smart_breakout_min_range):
        return {
            "engine": "smart_breakout_engine",
            "signal": "NONE",
            "strength": 0.08,
            "confidence": 15,
            "reason": "none",
        }

    body = abs(c_close - c_open)
    rng = max(1e-6, candle_range)
    strong_body = (body / rng) > 0.5
    upper_wick = c_high - max(c_open, c_close)
    lower_wick = min(c_open, c_close) - c_low
    fake_breakout_up = upper_wick > (body * 1.5)
    fake_breakout_down = lower_wick > (body * 1.5)

    breakout_up = c_high > p_high > 0
    breakout_down = c_low < p_low < 10_000_000
    volume_spike = avg_vol > 0 and c_vol > (avg_vol * float(settings.smart_breakout_volume_spike_ratio))
    if not volume_spike:
        return {
            "engine": "smart_breakout_engine",
            "signal": "NONE",
            "strength": 0.1,
            "confidence": 18,
            "reason": "none",
        }

    momentum = None
    strong_momentum_up = False
    strong_momentum_down = False
    if intrabar:
        try:
            ltp_now = float(intrabar.get("ltp_now") or 0.0)
            ltp_ago = float(intrabar.get("ltp_3sec_ago") or 0.0)
            momentum = ltp_now - ltp_ago
        except (TypeError, ValueError):
            momentum = None
        if momentum is not None:
            m_th = float(settings.smart_breakout_momentum_threshold)
            strong_momentum_up = momentum > m_th
            strong_momentum_down = momentum < -m_th

    # Case A: real breakout up
    if breakout_up and (not fake_breakout_up) and c_close > vwap_v and volume_spike and strong_body:
        strength = 0.84 if strong_momentum_up else 0.8
        confidence = 76 if strong_momentum_up else 68
        return {
            "engine": "smart_breakout_engine",
            "signal": "BUY_CE",
            "strength": round(strength, 3),
            "confidence": int(confidence),
            "reason": "real_breakout",
        }

    # Case B: real breakout down
    if breakout_down and (not fake_breakout_down) and c_close < vwap_v and volume_spike and strong_body:
        strength = 0.84 if strong_momentum_down else 0.8
        confidence = 76 if strong_momentum_down else 68
        return {
            "engine": "smart_breakout_engine",
            "signal": "BUY_PE",
            "strength": round(strength, 3),
            "confidence": int(confidence),
            "reason": "real_breakout",
        }

    # Case C: fake breakout trap / liquidity sweep reversal
    if breakout_up and fake_breakout_up:
        return {
            "engine": "smart_breakout_engine",
            "signal": "BUY_PE",
            "strength": 0.83,
            "confidence": 70,
            "reason": "fake_breakout",
        }
    if breakout_down and fake_breakout_down:
        return {
            "engine": "smart_breakout_engine",
            "signal": "BUY_CE",
            "strength": 0.83,
            "confidence": 70,
            "reason": "liquidity_sweep",
        }

    # Case D: no signal
    return {
        "engine": "smart_breakout_engine",
        "signal": "NONE",
        "strength": 0.12,
        "confidence": 20,
        "reason": "none",
    }


class SmartBreakoutEngine(BaseEngine):
    name = "smart_breakout_engine"

    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        candle = tick.meta.get("candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        if not candle or not prev:
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="none")

        sym = tick.symbol
        tf = str(tick.meta.get("timeframe") or "1m")
        avg_vol = self._avg_volume(sym, tf, candle, state)
        intrabar_data = tick.meta.get("intrabar_data") or None
        out = smart_breakout_engine(
            current_candle=candle,
            previous_candle=prev,
            vwap=float(tick.meta.get("vwap") or 0.0),
            avg_volume=avg_vol,
            intrabar_data=intrabar_data,
        )

        candle_id = str((candle.get("end") or candle.get("start") or ""))
        if out["signal"] != "NONE" and not self._allow_one_per_candle(sym, tf, candle_id, state):
            return EngineOutput(engine=self.name, signal="NONE", strength=0.0, confidence=0.0, reason="none")

        if out["signal"] != "NONE":
            self._mark_fired(sym, tf, candle_id, state)
            logger.info(
                "SMART BREAKOUT: type=%s symbol=%s price=%.2f conf=%s reason=%s",
                out.get("reason"),
                sym,
                float(tick.index_price),
                out.get("confidence"),
                out.get("reason"),
            )
        return EngineOutput(
            engine=self.name,
            signal=out["signal"],
            strength=float(out["strength"]),
            confidence=float(out["confidence"]),
            reason=str(out["reason"]),
        )

    def _avg_volume(self, symbol: str, timeframe: str, candle: Dict, state: Dict) -> float:
        k = f"{symbol}:{timeframe}"
        vol_hist: Dict[str, Deque[float]] = state.setdefault("smart_breakout_vol_hist", {})
        row = vol_hist.setdefault(k, deque(maxlen=25))
        avg = (sum(row) / len(row)) if row else float(candle.get("volume") or 0.0)
        row.append(float(candle.get("volume") or 0.0))
        return avg

    @staticmethod
    def _allow_one_per_candle(symbol: str, timeframe: str, candle_id: str, state: Dict) -> bool:
        last = state.setdefault("smart_breakout_last_candle", {}).get(f"{symbol}:{timeframe}")
        return last != candle_id

    @staticmethod
    def _mark_fired(symbol: str, timeframe: str, candle_id: str, state: Dict) -> None:
        state.setdefault("smart_breakout_last_candle", {})[f"{symbol}:{timeframe}"] = candle_id
