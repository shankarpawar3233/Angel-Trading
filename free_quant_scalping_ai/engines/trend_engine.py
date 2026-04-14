from __future__ import annotations

from engines.base import BaseEngine
from engines.config import engine_enabled
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class TrendEngine(BaseEngine):
    name = "trend"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("trend"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name}, intent="TREND")

        hist = list(market_state.price_history or [])
        price = float(market_state.price or 0.0)
        if len(hist) < 26 or price <= 0:
            return self._out("NO_TRADE", 0.0, "insufficient_history", {}, intent="TREND")

        short = hist[-9:]
        long = hist[-21:]
        short_ma = sum(short) / max(1, len(short))
        long_ma = sum(long) / max(1, len(long))
        spread = short_ma - long_ma
        norm = abs(spread) / max(1.0, price)

        if spread > 0:
            signal = "BUY_CE"
            confidence = min(85.0, 52.0 + norm * 18000.0)
            reason = f"short_ma_above_long|spread={spread:.2f}"
        elif spread < 0:
            signal = "BUY_PE"
            confidence = min(85.0, 52.0 + norm * 18000.0)
            reason = f"short_ma_below_long|spread={spread:.2f}"
        else:
            signal = "NO_TRADE"
            confidence = 0.0
            reason = "flat_spread"

        out = self._out(
            signal,
            float(round(confidence, 2)),
            reason,
            {"short_ma": round(short_ma, 2), "long_ma": round(long_ma, 2), "spread": round(spread, 4)},
            intent="TREND",
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
