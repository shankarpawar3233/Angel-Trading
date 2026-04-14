from __future__ import annotations

from engines.base import BaseEngine
from engines.config import engine_enabled
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class OiEngine(BaseEngine):
    name = "oi"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("oi"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name}, intent="OI")

        oi = dict(market_state.oi_snap or {})
        ce = float(oi.get("ce_oi_total") or 0.0)
        pe = float(oi.get("pe_oi_total") or 0.0)
        pcr = float(oi.get("pcr") or 0.0)
        if ce <= 0 and pe <= 0:
            return self._out("NO_TRADE", 0.0, "oi_unavailable", {"pcr": pcr}, intent="OI")

        if pcr >= 1.15:
            signal = "BUY_CE"
            confidence = min(82.0, 50.0 + min(25.0, (pcr - 1.15) * 120.0))
            reason = f"bullish_pcr|pcr={pcr:.2f}"
        elif pcr <= 0.85:
            signal = "BUY_PE"
            confidence = min(82.0, 50.0 + min(25.0, (0.85 - pcr) * 120.0))
            reason = f"bearish_pcr|pcr={pcr:.2f}"
        else:
            signal = "NO_TRADE"
            confidence = 0.0
            reason = f"neutral_pcr|pcr={pcr:.2f}"

        out = self._out(
            signal,
            float(round(confidence, 2)),
            reason,
            {
                "pcr": round(pcr, 4),
                "ce_oi_total": round(ce, 2),
                "pe_oi_total": round(pe, 2),
                "max_oi_call": oi.get("max_oi_call"),
                "max_oi_put": oi.get("max_oi_put"),
            },
            intent="OI",
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
