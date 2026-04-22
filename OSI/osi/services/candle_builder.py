from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from osi.core.models import Candle, MarketTick


@dataclass
class _Bucket:
    bucket_id: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    start: datetime
    end: datetime
    option_chain: Dict


class CandleBuilder:
    """
    Builds 1m, 3m and 5m candles from live ticks.
    Engines should run only when a candle closes.
    """

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, _Bucket]] = {}
        self._last_closed: Dict[str, Dict[str, Candle]] = {}

    def add_tick(self, tick: MarketTick) -> List[Candle]:
        closed: List[Candle] = []
        symbol_state = self._state.setdefault(tick.symbol, {})
        for timeframe, minutes in (("1m", 1), ("3m", 3), ("5m", 5)):
            closed.extend(self._handle_timeframe(symbol_state, tick, timeframe, minutes))
        return closed

    def _handle_timeframe(self, symbol_state: Dict[str, _Bucket], tick: MarketTick, timeframe: str, minutes: int) -> List[Candle]:
        out: List[Candle] = []
        key = timeframe
        bucket = symbol_state.get(key)
        bucket_id = self._bucket_id(tick.timestamp, minutes)
        frame_start = self._bucket_start(bucket_id, minutes)
        frame_end = frame_start + timedelta(minutes=minutes)

        if bucket is None:
            symbol_state[key] = _Bucket(
                bucket_id=bucket_id,
                open=tick.index_price,
                high=tick.index_price,
                low=tick.index_price,
                close=tick.index_price,
                volume=float(tick.meta.get("volume", 0.0) or 0.0),
                start=frame_start,
                end=frame_end,
                option_chain=tick.option_chain,
            )
            return out

        if bucket_id != bucket.bucket_id:
            closed = Candle(
                symbol=tick.symbol,
                timeframe=timeframe,
                open=bucket.open,
                high=bucket.high,
                low=bucket.low,
                close=bucket.close,
                volume=bucket.volume,
                start=bucket.start,
                end=bucket.end,
                option_chain=bucket.option_chain,
                meta={"closed_at": datetime.now(timezone.utc).isoformat()},
            )
            out.append(closed)
            self._last_closed.setdefault(tick.symbol, {})[timeframe] = closed
            symbol_state[key] = _Bucket(
                bucket_id=bucket_id,
                open=tick.index_price,
                high=tick.index_price,
                low=tick.index_price,
                close=tick.index_price,
                volume=float(tick.meta.get("volume", 0.0) or 0.0),
                start=frame_start,
                end=frame_end,
                option_chain=tick.option_chain,
            )
            return out

        bucket.high = max(bucket.high, tick.index_price)
        bucket.low = min(bucket.low, tick.index_price)
        bucket.close = tick.index_price
        bucket.volume += float(tick.meta.get("volume", 0.0) or 0.0)
        bucket.option_chain = tick.option_chain
        return out

    @staticmethod
    def _bucket_id(ts: datetime, minutes: int) -> int:
        t = ts.astimezone(timezone.utc)
        return int(t.timestamp()) // (minutes * 60)

    @staticmethod
    def _bucket_start(bucket_id: int, minutes: int) -> datetime:
        return datetime.fromtimestamp(bucket_id * minutes * 60, tz=timezone.utc)

    def get_live_candle(self, symbol: str, timeframe: str) -> Dict | None:
        bucket = (self._state.get(symbol) or {}).get(timeframe)
        if not bucket:
            return None
        return {
            "open": bucket.open,
            "high": bucket.high,
            "low": bucket.low,
            "close": bucket.close,
            "volume": bucket.volume,
            "start": bucket.start,
            "end": bucket.end,
            "bucket_id": bucket.bucket_id,
        }

    def get_last_closed(self, symbol: str, timeframe: str) -> Dict | None:
        c = (self._last_closed.get(symbol) or {}).get(timeframe)
        if c is None:
            return None
        return c.model_dump(mode="json")

