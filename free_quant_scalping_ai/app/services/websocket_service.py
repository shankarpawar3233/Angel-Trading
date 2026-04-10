"""
Thin facade over Angel WebSocket caches for the live signal path.
Keeps ``api_server`` free of low-level cache imports.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from data.angel_ws_manager import get_feed_health, get_option_chain_copy, get_ws_price


def get_index_ltp(symbol: str) -> Optional[float]:
    return get_ws_price(symbol)


def get_option_chain_snapshot(symbol: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    return get_option_chain_copy(symbol)


def get_feed_health_snapshot() -> Dict[str, Any]:
    return get_feed_health()
