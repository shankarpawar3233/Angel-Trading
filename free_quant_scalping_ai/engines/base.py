from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from engines.market_state import MarketState

EngineOutput = Dict[str, Any]


class BaseEngine(ABC):
    """Each engine returns a normalized opinion dict (no side effects required)."""

    name: str = "base"

    @abstractmethod
    def process_tick(self, market_state: MarketState) -> EngineOutput:
        """
        Returns:
            signal: BUY_CE | BUY_PE | NO_TRADE
            confidence: float 0..100
            reason: str
            metadata: dict
        """
        raise NotImplementedError

    def _out(
        self,
        signal: str,
        confidence: float,
        reason: str,
        metadata: Dict[str, Any] | None = None,
        intent: str = "SCALP",
    ) -> EngineOutput:
        return {
            "signal": signal,
            "confidence": float(confidence),
            "reason": reason,
            "intent": intent,
            "metadata": metadata or {},
        }
