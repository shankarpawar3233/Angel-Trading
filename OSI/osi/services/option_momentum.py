from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Tuple

from osi.core.config import settings
from osi.core.models import MarketTick, SignalType


class OptionMomentumService:
    def __init__(self) -> None:
        self._hist: Dict[str, Deque[Tuple[float, float]]] = {}

    def evaluate(
        self,
        *,
        tick: MarketTick,
        option_symbol: str,
        option_ltp: float,
        signal: SignalType,
    ) -> Dict[str, float | bool]:
        now_ts = self._ts(tick.timestamp)
        self._append(option_symbol, now_ts, float(option_ltp))
        cur_delta = self._delta(option_symbol, now_ts, int(settings.option_momentum_lookback_seconds))
        prev_delta = self._delta(option_symbol, now_ts - 1.0, int(settings.option_momentum_lookback_seconds))
        accel = cur_delta - prev_delta

        opposite_symbol, opposite_ltp = self._opposite_leg(tick, option_symbol)
        opp_delta = 0.0
        if opposite_symbol and opposite_ltp is not None:
            self._append(opposite_symbol, now_ts, float(opposite_ltp))
            opp_delta = self._delta(opposite_symbol, now_ts, int(settings.option_momentum_lookback_seconds))

        min_delta = float(settings.option_momentum_min_delta)
        if signal == "BUY_CE":
            confirmed = cur_delta >= min_delta and accel >= 0 and opp_delta <= (min_delta * 0.5)
        elif signal == "BUY_PE":
            confirmed = cur_delta >= min_delta and accel >= 0 and opp_delta <= (min_delta * 0.5)
        else:
            confirmed = False

        strength = max(0.0, min(1.0, (cur_delta / max(min_delta, 1e-6)) * 0.7 + max(0.0, accel) * 0.3))
        return {
            "option_momentum_confirmed": bool(confirmed),
            "momentum_strength": round(strength, 3),
            "option_ltp_change": round(cur_delta, 3),
            "option_acceleration": round(accel, 3),
            "opposite_option_change": round(opp_delta, 3),
        }

    def _append(self, key: str, ts: float, price: float) -> None:
        hist = self._hist.setdefault(key, deque(maxlen=200))
        hist.append((ts, price))

    def _delta(self, key: str, now_ts: float, lookback_sec: int) -> float:
        hist = self._hist.get(key)
        if not hist:
            return 0.0
        target = now_ts - max(1, lookback_sec)
        base = hist[0][1]
        for ts, px in hist:
            if ts >= target:
                base = px
                break
        current = hist[-1][1]
        return max(0.0, current - base)

    @staticmethod
    def _ts(ts: datetime) -> float:
        if isinstance(ts, datetime):
            return ts.timestamp()
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _opposite_leg(tick: MarketTick, option_symbol: str) -> Tuple[str | None, float | None]:
        parts = option_symbol.split("_")
        if len(parts) < 4:
            return None, None
        expiry = parts[1]
        strike = parts[2]
        leg = parts[3]
        opposite = "PE" if leg == "CE" else "CE"
        row = ((tick.option_chain or {}).get(expiry) or {}).get(strike)
        if not row:
            return None, None
        px = (row.get(opposite) or {}).get("ltp")
        if px is None:
            return None, None
        opposite_symbol = f"{parts[0]}_{parts[1]}_{strike}_{opposite}"
        return opposite_symbol, float(px)
