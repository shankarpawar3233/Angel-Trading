"""
Optional NIFTY option chain from NSE India (HTTP), same shape as WS cache.

Use when Angel WebSocket option ticks are unavailable. Data is delayed vs live ticks;
NSE may rate-limit or change endpoints — not for redistribution. Enable with:

    OPTION_CHAIN_FALLBACK_NSE=1
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Any] = {"ts": 0.0, "chain": {}}
_TTL_SEC = 15.0
_NSE_HOME = "https://www.nseindia.com/"
_NSE_API = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"


def _leg(side: Dict[str, Any]) -> Dict[str, Any]:
    if not side or not isinstance(side, dict):
        return {}
    ltp = side.get("lastPrice")
    if ltp is None:
        return {}
    ch = side.get("changeinOpenInterest") or side.get("changeInOpenInterest")
    return {
        "ltp": float(ltp) if ltp is not None else None,
        "oi": float(side["openInterest"]) if side.get("openInterest") is not None else None,
        "volume": int(side.get("totalTradedVolume") or 0),
        "change_oi": float(ch) if ch is not None else None,
        "oi_change": float(ch) if ch is not None else None,
        "token": "nse",
        "source": "nse",
    }


def fetch_nifty_option_chain_nse(force: bool = False) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Strike-keyed chain: {\"23700\": {\"CE\": {...}, \"PE\": {...}}, ...}
    """
    global _CACHE
    now = time.time()
    if not force and _CACHE["chain"] and (now - float(_CACHE["ts"])) < _TTL_SEC:
        return dict(_CACHE["chain"])  # type: ignore

    try:
        import requests
    except ImportError:
        logger.warning("[NSE] requests not installed")
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    session = requests.Session()
    session.headers.update(headers)
    try:
        session.get(_NSE_HOME, timeout=12)
        r = session.get(_NSE_API, timeout=18)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("[NSE] option chain request failed: %s", exc)
        return {}

    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    rows = (data.get("records") or {}).get("data") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sp = row.get("strikePrice")
        if sp is None:
            continue
        try:
            sk = str(int(float(sp)))
        except (TypeError, ValueError):
            continue
        ce = _leg(row.get("CE") or {})
        pe = _leg(row.get("PE") or {})
        if not ce and not pe:
            continue
        out[sk] = {}
        if ce:
            out[sk]["CE"] = ce
        if pe:
            out[sk]["PE"] = pe

    _CACHE["ts"] = now
    _CACHE["chain"] = out
    logger.info("[NSE] option chain rows (strikes): %s", len(out))
    return dict(out)


def nse_fallback_enabled() -> bool:
    import os

    v = os.getenv("OPTION_CHAIN_FALLBACK_NSE", "").strip().lower()
    return v in ("1", "true", "yes", "on")
