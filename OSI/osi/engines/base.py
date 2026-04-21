from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

from osi.core.models import EngineOutput, MarketTick


class BaseEngine(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate(self, tick: MarketTick, state: Dict) -> EngineOutput:
        raise NotImplementedError

    @staticmethod
    def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))

