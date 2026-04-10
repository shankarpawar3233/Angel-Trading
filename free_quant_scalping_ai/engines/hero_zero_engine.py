from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from engines.base import BaseEngine
from engines.config import engine_enabled, env_float
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState
from utils.logger import get_logger

try:
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone(timedelta(hours=5, minutes=30))

logger = get_logger(__name__)

# NIFTY weekly expiry: Thursday (weekday 0=Mon … 3=Thu)
# SENSEX (BFO): Friday (4=Fri)
_NIFTY_EXPIRY_WEEKDAY = 3
_SENSEX_EXPIRY_WEEKDAY = 4


def _truthy_env(key: str) -> bool:
    return (os.getenv(key) or "").strip().lower() in ("1", "true", "yes", "on")


def hero_zero_gate(symbol: str) -> tuple[bool, str]:
    """
    Returns (active, reason) for HeroZeroEngine day-gate only.
    reason: "forced" | "expiry_day" | "disabled"
    """
    su = str(symbol or "").strip().upper()
    if _truthy_env("ENGINE_HEROZERO_FORCE"):
        return True, "forced"
    explicit = os.getenv(f"{su}_EXPIRY_DATE", "").strip()
    if explicit:
        try:
            d = datetime.strptime(explicit, "%Y-%m-%d").date()
            return (
                (True, "expiry_day")
                if datetime.now(_IST).date() == d
                else (False, "disabled")
            )
        except ValueError:
            pass  # malformed date: fall through to weekday / index rules
    wd = os.getenv(f"{su}_EXPIRY_WEEKDAY")
    if wd is not None and str(wd).strip() != "":
        try:
            target = int(wd)
            return (
                (True, "expiry_day")
                if datetime.now(_IST).weekday() == target
                else (False, "disabled")
            )
        except ValueError:
            pass  # fall through
    if su == "NIFTY":
        if datetime.now(_IST).weekday() == _NIFTY_EXPIRY_WEEKDAY:
            return True, "expiry_day"
        return False, "disabled"
    if su == "SENSEX":
        if datetime.now(_IST).weekday() == _SENSEX_EXPIRY_WEEKDAY:
            return True, "expiry_day"
        return False, "disabled"
    return False, "disabled"


def _is_expiry_day_for_symbol(symbol: str) -> bool:
    active, _ = hero_zero_gate(symbol)
    return active


class HeroZeroEngine(BaseEngine):
    name = "hero_zero"

    def process_tick(self, market_state: MarketState):
        sym = str(market_state.underlying or market_state.symbol or "").strip().upper()

        def _meta(extra: dict | None = None) -> dict:
            m = {"engine": self.name, "symbol": sym}
            if extra:
                m.update(extra)
            return m

        if not engine_enabled("hero_zero"):
            logger.debug(
                "[HeroZeroEngine] hero_zero_active=false reason=disabled (engine_disabled) symbol=%s",
                sym,
            )
            return self._out(
                "NO_TRADE",
                0.0,
                "engine_disabled",
                _meta({"hero_zero_active": False, "reason": "disabled"}),
                intent="LOTTERY",
            )

        gate_ok, gate_reason = hero_zero_gate(sym)
        logger.debug(
            "[HeroZeroEngine] hero_zero_active=%s reason=%s symbol=%s",
            gate_ok,
            gate_reason,
            sym,
        )
        if not gate_ok:
            return self._out(
                "NO_TRADE",
                0.0,
                "not_expiry_day",
                _meta({"hero_zero_active": False, "reason": "disabled"}),
                intent="LOTTERY",
            )

        r = market_state.scalping_pipeline_result or {}
        hz = r.get("hero_zero")
        if not hz:
            return self._out(
                "NO_TRADE",
                35.0,
                "no_hero_zero_candidate",
                _meta({"hero_zero_active": True, "reason": gate_reason}),
                intent="LOTTERY",
            )

        strength = float(hz.get("strength") or 0.0)
        min_s = env_float("HEROZERO_ENGINE_MIN_STRENGTH", 55.0)
        if strength < min_s:
            out = self._out(
                "NO_TRADE",
                min(55.0, 25.0 + strength * 0.35),
                "strength_below_threshold",
                _meta({"hero": hz, "hero_zero_active": True, "reason": gate_reason}),
                intent="LOTTERY",
            )
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        typ = str(hz.get("type") or "").upper()
        if typ == "CE":
            sig = "BUY_CE"
        elif typ == "PE":
            sig = "BUY_PE"
        else:
            out = self._out(
                "NO_TRADE",
                40.0,
                "unknown_hero_type",
                _meta({"hero": hz, "hero_zero_active": True, "reason": gate_reason}),
                intent="LOTTERY",
            )
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        conf = min(95.0, 50.0 + strength * 0.38)
        out = self._out(
            sig,
            conf,
            "hero_zero_expiry_spike",
            _meta(
                {
                    "hero": hz,
                    "expiry_day": True,
                    "hero_zero_active": True,
                    "reason": gate_reason,
                }
            ),
            intent="LOTTERY",
        )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
