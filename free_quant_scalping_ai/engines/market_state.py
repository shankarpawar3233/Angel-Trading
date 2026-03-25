from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MarketState:
    """
    Shared tick snapshot + mutable refs (decision_ds, side_balance) aligned with API global state.
    ``scalping_pipeline_result`` is filled once per tick before engines run.
    """

    symbol: str
    underlying: str
    price: float
    chain: Dict[str, Dict[str, Dict[str, Any]]]
    price_history: List[float]
    now_epoch: float
    previous_index_price: Optional[float]
    decision_ds: Dict[str, Any]
    side_balance_state: Dict[str, Dict[str, float]]
    oi_snap: Dict[str, Any]
    scalping_pipeline_result: Optional[Dict[str, Any]] = None
    _feature_cache: Dict[str, Any] = field(default_factory=dict)

    def cache_get(self, key: str, factory):
        if key not in self._feature_cache:
            self._feature_cache[key] = factory()
        return self._feature_cache[key]
