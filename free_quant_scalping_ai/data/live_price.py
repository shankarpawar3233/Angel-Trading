"""
Live index prices from Angel WebSocket cache only (no REST LTP polling).

Session login still uses SmartAPI once at startup for feed_token; prices come from ticks.
Set NIFTY_PROXY_SPOT for option discovery before the first NIFTY tick arrives.
"""

from __future__ import annotations

from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger(__name__)


def get_live_price(symbol: str) -> Dict[str, Any]:
    """
    Non-blocking read from in-memory WebSocket cache.

    Returns:
        {"symbol": "NIFTY", "price": float|None, "source": "angel_ws"|"angel_ws_pending"}
    """
    from data.angel_ws_manager import get_ws_price

    s = (symbol or "").strip().upper()
    p = get_ws_price(s)
    if p is not None:
        return {"symbol": s, "price": float(p), "source": "angel_ws"}
    return {"symbol": s, "price": None, "source": "angel_ws_pending"}
