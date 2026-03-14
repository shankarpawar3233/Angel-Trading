from __future__ import annotations

"""
NSE live index price fetcher using the public allIndices endpoint.

This is used as the primary source of live prices for NIFTY and SENSEX.
"""

from typing import Any, Dict, Optional

import requests

from config.settings import settings
from utils.logger import get_logger


logger = get_logger(__name__)

_SESSION: requests.Session | None = None

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

_INDEX_NAME_MAP: Dict[str, str] = {
    # Map internal symbols to NSE index names in allIndices payload
    "NIFTY": "NIFTY 50",
    "SENSEX": "SENSEX",  # included in allIndices payload
}


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_HEADERS)
        # Warm up session to obtain cookies; NSE often returns {} without cookies.
        try:
            _SESSION.get("https://www.nseindia.com/", timeout=10)
        except Exception:
            pass
    return _SESSION


def get_index_price(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch live index price from NSE allIndices endpoint.

    Returns:
        {"symbol": "NIFTY", "price": 23680.5, "source": "nse_live"} or None on failure.
    """
    nse_name = _INDEX_NAME_MAP.get(symbol.upper())
    if not nse_name:
        logger.warning("NSE live price: unsupported symbol %s", symbol)
        return None

    url = "https://www.nseindia.com/api/allIndices"
    try:
        sess = _get_session()
        resp = sess.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning("[NSE] allIndices HTTP %s", resp.status_code)
            return None
        data = resp.json()
        indices = data.get("data") or data.get("indices") or []
        for idx in indices:
            if idx.get("index") == nse_name:
                price = idx.get("last") or idx.get("lastPrice")
                if price is None:
                    continue
                logger.info("[NSE] Live price fetched for %s: %s", symbol, price)
                return {"symbol": symbol, "price": float(price), "source": "nse_live"}
        logger.warning("[NSE] Index name %s not found in allIndices payload", nse_name)
    except Exception as exc:
        logger.warning("[NSE] Live price fetch error for %s: %s", symbol, exc)
    return None

