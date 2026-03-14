from __future__ import annotations

"""
Angel One instrument master loader and symbol lookup.

Loads OpenAPIScripMaster.json, caches it under storage/angel_instruments.json,
and provides a helper to resolve exchange/token for symbols like NIFTY, SENSEX.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config.settings import DATA_DIR
from utils.logger import get_logger


logger = get_logger(__name__)

INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENTS_PATH = Path(DATA_DIR) / "angel_instruments.json"

_INSTRUMENT_CACHE: Optional[List[Dict[str, Any]]] = None


def _download_instruments() -> List[Dict[str, Any]]:
    logger.info("[ANGEL] Downloading instrument master file from %s", INSTRUMENTS_URL)
    resp = requests.get(INSTRUMENTS_URL, timeout=15)
    resp.raise_for_status()
    instruments = resp.json()
    INSTRUMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INSTRUMENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(instruments, f)
    logger.info("[ANGEL] Instrument master file loaded and cached at %s", INSTRUMENTS_PATH)
    return instruments


def load_instruments() -> List[Dict[str, Any]]:
    """
    Load Angel instrument master list, from cache if available, otherwise from web.
    """
    global _INSTRUMENT_CACHE
    if _INSTRUMENT_CACHE is not None:
        return _INSTRUMENT_CACHE

    if INSTRUMENTS_PATH.exists():
        try:
            with open(INSTRUMENTS_PATH, "r", encoding="utf-8") as f:
                _INSTRUMENT_CACHE = json.load(f)
            logger.info("[ANGEL] Instrument master file loaded from cache %s", INSTRUMENTS_PATH)
            return _INSTRUMENT_CACHE
        except Exception as exc:
            logger.warning("[ANGEL] Failed to read cached instrument file, re-downloading: %s", exc)

    _INSTRUMENT_CACHE = _download_instruments()
    return _INSTRUMENT_CACHE


def get_instrument(symbol: str) -> Optional[Dict[str, str]]:
    """
    Resolve exchange and token for a given symbol using the instrument master.

    Returns:
        {"exchange": "NSE", "token": "26000"} or None if not found.
    """
    instruments = load_instruments()
    symbol_u = symbol.upper()
    found: Optional[Dict[str, Any]] = None

    for inst in instruments:
        # Typical Angel fields: symbol, name, exch_seg, token
        if str(inst.get("symbol", "")).upper() == symbol_u:
            found = inst
            break

    if not found:
        logger.warning("[ANGEL] Instrument not found for symbol %s in master file", symbol)
        return None

    exch = found.get("exch_seg")
    token = found.get("token")
    if not exch or not token:
        logger.warning("[ANGEL] Instrument record for %s missing exch_seg/token: %s", symbol, found)
        return None

    logger.info("[ANGEL] Token resolved for %s: exchange=%s token=%s", symbol, exch, token)
    return {"exchange": str(exch), "token": str(token)}

