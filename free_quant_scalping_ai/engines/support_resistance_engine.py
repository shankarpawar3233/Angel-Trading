from __future__ import annotations

from engines.base import BaseEngine
from engines.cache import compute_atm_strike
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class SupportResistanceEngine(BaseEngine):
    name = "support_resistance"

    def process_tick(self, market_state: MarketState):
        hist = market_state.price_history
        price = float(market_state.price or 0.0)
        if len(hist) < 8 or price <= 0:
            out = self._out("NO_TRADE", 0.0, "insufficient_history", {}, intent="BREAKOUT")
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        lookback = min(120, len(hist))
        win = hist[-lookback:]
        day_low = min(win)
        day_high = max(win)
        vwap = sum(win) / max(1, len(win))

        oi = market_state.oi_snap or {}
        oi_support = oi.get("max_oi_put")
        oi_resistance = oi.get("max_oi_call")

        # OI walls take precedence when present.
        support = float(oi_support) if oi_support is not None else float(day_low)
        resistance = float(oi_resistance) if oi_resistance is not None else float(day_high)
        if support >= resistance:
            support, resistance = float(day_low), float(day_high)

        dist_support = price - support
        dist_resistance = resistance - price
        spread = max(1.0, resistance - support)
        proximity = min(dist_support, dist_resistance) / spread

        if dist_support < 0:
            sig = "BUY_PE"
            reason = "below_support_breakdown"
            conf = 60.0
        elif dist_resistance < 0:
            sig = "BUY_CE"
            reason = "above_resistance_breakout"
            conf = 60.0
        elif proximity <= 0.12:
            # Near edge: reversal bias.
            if dist_support < dist_resistance:
                sig = "BUY_CE"
                reason = "near_support_reversal_zone"
            else:
                sig = "BUY_PE"
                reason = "near_resistance_reversal_zone"
            conf = 56.0
        else:
            sig = "NO_TRADE"
            reason = "mid_range_no_edge"
            conf = 42.0

        out = self._out(
            sig,
            conf,
            reason,
            {
                "support": round(support, 2),
                "resistance": round(resistance, 2),
                "distance_support": round(dist_support, 2),
                "distance_resistance": round(dist_resistance, 2),
                "intraday_high": round(day_high, 2),
                "intraday_low": round(day_low, 2),
                "vwap_proxy": round(vwap, 2),
                "atm": compute_atm_strike(market_state.chain, price),
            },
            intent="REVERSAL" if "reversal" in reason else "BREAKOUT",
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
