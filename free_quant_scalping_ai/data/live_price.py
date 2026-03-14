"""
Live price helper for indices.

Primary source: NSE allIndices endpoint via data.nse_live_price.
Fallback: last stored candle from SQLite (cached).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from data.angel_live_price import get_angel_price
from data.data_storage import load_candles
from data.nse_live_price import get_index_price
from utils.logger import get_logger

logger = get_logger(__name__)


def get_live_price(symbol: str) -> Dict[str, Any]:
    """
    Return current price info for index symbol.

    Tries Angel SmartAPI first, then NSE live feed, then falls back to last stored candle.

    Returns:
        {"symbol": "NIFTY", "price": 23680.5, "source": "angel_smartapi"|"nse_live"|"cached"}
    """
    # 1) Try Angel SmartAPI
    angel = get_angel_price(symbol)
    if angel and angel.get("price") is not None:
        return angel

    # 2) Try NSE live feed
    live = get_index_price(symbol)
    if live and live.get("price") is not None:
        return live

    # 3) Fallback to last stored candle in DB
    candles = load_candles(symbol, "1m", limit=1)
    if candles.empty:
        candles = load_candles(symbol, "5m", limit=1)
    if candles.empty:
        candles = load_candles(symbol, "15m", limit=1)

    price: Optional[float] = None
    if not candles.empty:
        price = float(candles["close"].iloc[-1])

    if price is not None:
        logger.info("Using cached price for %s from candles: %s", symbol, price)
    else:
        logger.warning("No cached price available for %s", symbol)

    return {"symbol": symbol, "price": price, "source": "cached"}

