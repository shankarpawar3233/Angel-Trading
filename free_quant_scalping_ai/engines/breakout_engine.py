from __future__ import annotations

from engines.base import BaseEngine
from engines.config import engine_enabled
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class BreakoutEngine(BaseEngine):
    name = "breakout"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("breakout"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name}, intent="BREAKOUT")

        hist = list(market_state.price_history or [])
        price = float(market_state.price or 0.0)
        if len(hist) < 20 or price <= 0:
            return self._out("NO_TRADE", 0.0, "insufficient_history", {}, intent="BREAKOUT")

        win = hist[-20:-1]
        if not win:
            return self._out("NO_TRADE", 0.0, "insufficient_window", {}, intent="BREAKOUT")
        hi = max(win)
        lo = min(win)
        rng = max(hi - lo, 1e-6)
        up_break = price > hi
        down_break = price < lo

        if up_break and not down_break:
            conf = min(84.0, 55.0 + ((price - hi) / rng) * 140.0)
            out = self._out("BUY_CE", float(round(conf, 2)), f"range_break_up|{price - hi:.2f}", {"high": hi, "low": lo}, intent="BREAKOUT")
        elif down_break and not up_break:
            conf = min(84.0, 55.0 + ((lo - price) / rng) * 140.0)
            out = self._out("BUY_PE", float(round(conf, 2)), f"range_break_down|{lo - price:.2f}", {"high": hi, "low": lo}, intent="BREAKOUT")
        else:
            out = self._out("NO_TRADE", 0.0, "inside_recent_range", {"high": hi, "low": lo}, intent="BREAKOUT")

        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
