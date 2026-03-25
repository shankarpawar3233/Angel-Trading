from __future__ import annotations

from engines.base import BaseEngine
from engines.cache import compute_atm_strike
from engines.config import engine_enabled, env_float, env_int
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class HoldEngine(BaseEngine):
    """
    Slower bias: EMA vs spot, longer momentum window, simple VWAP proxy (session mean).
    Requires consecutive bars agreeing before emitting a side.
    """

    name = "hold"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("hold"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name})
        hist = market_state.price_history
        price = float(market_state.price or 0.0)
        if len(hist) < env_int("HOLD_MIN_HISTORY", 32) or price <= 0:
            return self._out("NO_TRADE", 0.0, "insufficient_history", {})

        span = env_int("HOLD_EMA_SPAN", 24)
        ema_slice = hist[-max(span * 3, span + 2) :]
        alpha = 2.0 / (span + 1.0)
        ema = ema_slice[0]
        for p in ema_slice[1:]:
            ema = alpha * p + (1.0 - alpha) * ema
        vwap_proxy = sum(ema_slice) / len(ema_slice)

        long_win = env_int("HOLD_MOM_WIN", 36)
        mom = 0.0
        if len(hist) >= long_win:
            a, b = hist[-long_win], hist[-1]
            if a > 0:
                mom = (b - a) / a

        eps = env_float("HOLD_EMA_EPS", 0.00015)
        mom_up = env_float("HOLD_MOM_UP", 0.0002)
        mom_dn = env_float("HOLD_MOM_DOWN", -0.0002)
        need = env_int("HOLD_CONFIRM_BARS", 4)

        bullish = (price > ema * (1.0 + eps)) and (price >= vwap_proxy) and (mom >= mom_up)
        bearish = (price < ema * (1.0 - eps)) and (price <= vwap_proxy) and (mom <= mom_dn)

        if not bullish and not bearish:
            out = self._out("NO_TRADE", 45.0, "neutral_trend_vwap", {"ema": ema, "vwap_proxy": vwap_proxy, "mom": mom})
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        mono_slice = hist[-need:]
        mono_up = all(mono_slice[i] >= mono_slice[i - 1] for i in range(1, len(mono_slice)))
        mono_dn = all(mono_slice[i] <= mono_slice[i - 1] for i in range(1, len(mono_slice)))
        if bullish and not mono_up:
            out = self._out(
                "NO_TRADE",
                40.0,
                f"no_monotone_up_{need}b",
                {"bullish": bullish, "bearish": bearish},
            )
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out
        if bearish and not mono_dn:
            out = self._out(
                "NO_TRADE",
                40.0,
                f"no_monotone_dn_{need}b",
                {"bullish": bullish, "bearish": bearish},
            )
            append_engine_log(self.name, {"symbol": market_state.symbol, **out})
            return out

        streak = need
        if bullish and not bearish:
            conf = min(88.0, 55.0 + streak * 3.0 + min(15.0, abs(mom) * 8000.0))
            out = self._out("BUY_CE", conf, "ema_vwap_mom_bullish", {"ema": ema, "mom": mom, "streak": streak})
        elif bearish and not bullish:
            conf = min(88.0, 55.0 + streak * 3.0 + min(15.0, abs(mom) * 8000.0))
            out = self._out("BUY_PE", conf, "ema_vwap_mom_bearish", {"ema": ema, "mom": mom, "streak": streak})
        else:
            out = self._out("NO_TRADE", 42.0, "conflicting_bias", {})

        _ = compute_atm_strike(market_state.chain, price)
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
