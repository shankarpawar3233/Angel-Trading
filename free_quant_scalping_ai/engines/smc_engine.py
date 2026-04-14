from __future__ import annotations

from engines.base import BaseEngine
from engines.config import engine_enabled
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class SmcEngine(BaseEngine):
    """
    Lightweight SMC-style detector:
    - BOS (break of structure) on recent swing high/low
    - Liquidity sweep on prior extreme rejection
    """

    name = "smc"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("smc"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name}, intent="STRUCTURE")

        hist = list(market_state.price_history or [])
        price = float(market_state.price or 0.0)
        if len(hist) < 14 or price <= 0:
            return self._out("NO_TRADE", 0.0, "insufficient_history", {}, intent="STRUCTURE")

        lookback = min(12, max(6, len(hist) - 2))
        prior = hist[-(lookback + 1) : -1]
        prev = float(hist[-2]) if len(hist) >= 2 else price
        prev_high = max(prior) if prior else prev
        prev_low = min(prior) if prior else prev
        spread = max(prev_high - prev_low, 1e-6)

        broke_up = price > prev_high
        broke_down = price < prev_low
        swept_high = prev > prev_high and price <= prev_high
        swept_low = prev < prev_low and price >= prev_low

        signal = "NO_TRADE"
        confidence = 0.0
        reason = "no_structure_edge"

        if broke_up and not broke_down:
            signal = "BUY_CE"
            confidence = min(86.0, 58.0 + ((price - prev_high) / spread) * 110.0)
            reason = f"bos_up|break={price - prev_high:.2f}"
        elif broke_down and not broke_up:
            signal = "BUY_PE"
            confidence = min(86.0, 58.0 + ((prev_low - price) / spread) * 110.0)
            reason = f"bos_down|break={prev_low - price:.2f}"
        elif swept_low and not swept_high:
            signal = "BUY_CE"
            confidence = 56.0
            reason = "liquidity_sweep_low_reclaim"
        elif swept_high and not swept_low:
            signal = "BUY_PE"
            confidence = 56.0
            reason = "liquidity_sweep_high_reject"

        out = self._out(
            signal,
            float(round(confidence, 2)),
            reason,
            {
                "prev_high": round(prev_high, 2),
                "prev_low": round(prev_low, 2),
                "price": round(price, 2),
                "broke_up": bool(broke_up),
                "broke_down": bool(broke_down),
                "swept_high": bool(swept_high),
                "swept_low": bool(swept_low),
            },
            intent="STRUCTURE",
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
