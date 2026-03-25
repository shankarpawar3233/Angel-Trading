from __future__ import annotations

from engines.base import BaseEngine
from engines.config import engine_enabled
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class ScalpingEngine(BaseEngine):
    name = "scalping"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("scalping"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name})
        r = market_state.scalping_pipeline_result
        if not r:
            return self._out("NO_TRADE", 0.0, "no_scalping_result", {})
        sig = str(r.get("scalping_signal") or "NO_TRADE")
        conf = float(r.get("confidence") or 0)
        reason = ";".join(r.get("debug") or []) or str(r.get("decision_reason") or "scalping")
        out = self._out(
            sig,
            conf,
            reason,
            {
                "entry_decision": r.get("entry_decision"),
                "decision_reason": r.get("decision_reason"),
                "stable_count": r.get("stable_count"),
                "ml": r.get("ml_signal"),
            },
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
