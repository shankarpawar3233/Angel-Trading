from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


SignalSide = Literal["BUY_CE", "BUY_PE", "NONE"]


@dataclass
class ScalpingSignal:
    symbol: str
    index_price: float
    trade: SignalSide
    strike: Optional[str]
    entry: Optional[float]
    target: Optional[float]
    stoploss: Optional[float]
    confidence: float
    generated_at: datetime


@dataclass
class HeroZeroSignal:
    symbol: str
    strike: str
    entry: float
    target: float
    stoploss: float
    probability: float
    expected_roi: float
    generated_at: datetime


def round_safe(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None

