from __future__ import annotations

from collections import deque
from statistics import fmean, pstdev
from typing import Deque, Dict, Literal

from osi.core.models import Candle


class MarketRegimeEngine:
    def __init__(self) -> None:
        self._hist: Dict[str, Deque[float]] = {}

    def detect(self, candle: Candle) -> Literal["TRENDING", "SIDEWAYS"]:
        hist = self._hist.setdefault(candle.symbol, deque(maxlen=40))
        hist.append(candle.close)
        if len(hist) < 12:
            return "SIDEWAYS"
        prices = list(hist)
        drift = abs(fmean(prices[-6:]) - fmean(prices[-12:-6]))
        vol = pstdev(prices[-12:]) + 1e-6
        return "TRENDING" if drift / vol > 0.75 else "SIDEWAYS"

