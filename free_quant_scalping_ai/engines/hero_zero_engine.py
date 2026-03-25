from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from engines.base import BaseEngine
from engines.config import engine_enabled, env_float
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState

try:
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone(timedelta(hours=5, minutes=30))


def _is_expiry_day_for_symbol(symbol: str) -> bool:
    if os.getenv("ENGINE_HEROZERO_FORCE", "").strip().lower() in ("1", "true", "yes"):
        return True
    explicit = os.getenv(f"{symbol.upper()}_EXPIRY_DATE", "").strip()
    if explicit:
        try:
            d = datetime.strptime(explicit, "%Y-%m-%d").date()
            return datetime.now(_IST).date() == d
        except ValueError:
            pass
    # NIFTY weekly: Thursday; SENSEX: Friday (BSE) — override with SYMBOL_EXPIRY_WEEKDAY 0=Mon
    wd = os.getenv(f"{symbol.upper()}_EXPIRY_WEEKDAY")
    if wd is not None and str(wd).strip() != "":
        try:
            target = int(wd)
            return datetime.now(_IST).weekday() == target
        except ValueError:
            pass
    if symbol.upper() == "NIFTY":
        return datetime.now(_IST).weekday() == 3
    if symbol.upper() == "SENSEX":
        return datetime.now(_IST).weekday() == 4
    return False


class HeroZeroEngine(BaseEngine):
    name = "hero_zero"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("hero_zero"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name})
        if not _is_expiry_day_for_symbol(market_state.symbol):
            return self._out("NO_TRADE", 0.0, "not_expiry_day", {"engine": self.name})

        r = market_state.scalping_pipeline_result or {}
        hz = r.get("hero_zero")
        if not hz:
            return self._out("NO_TRADE", 35.0, "no_hero_zero_candidate", {})

        strength = float(hz.get("strength") or 0.0)
        min_s = env_float("HEROZERO_ENGINE_MIN_STRENGTH", 55.0)
        if strength < min_s:
            out = self._out("NO_TRADE", min(55.0, 25.0 + strength * 0.35), "strength_below_threshold", {"hero": hz})
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        typ = str(hz.get("type") or "").upper()
        if typ == "CE":
            sig = "BUY_CE"
        elif typ == "PE":
            sig = "BUY_PE"
        else:
            out = self._out("NO_TRADE", 40.0, "unknown_hero_type", {"hero": hz})
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        conf = min(95.0, 50.0 + strength * 0.38)
        out = self._out(
            sig,
            conf,
            "hero_zero_expiry_spike",
            {"hero": hz, "expiry_day": True},
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
