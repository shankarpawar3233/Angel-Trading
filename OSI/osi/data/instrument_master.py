from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from osi.core.config import settings

logger = logging.getLogger(__name__)

INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENTS_PATH = Path(__file__).resolve().parents[2] / "storage" / "angel_instruments.json"

_INSTRUMENT_CACHE: Optional[List[Dict[str, Any]]] = None


def _max_age_hours() -> float:
    return max(1.0, float(getattr(settings, "instrument_max_age_hours", 24.0) or 24.0))


def _cache_age_hours() -> float | None:
    if not INSTRUMENTS_PATH.exists():
        return None
    return max(0.0, (time.time() - INSTRUMENTS_PATH.stat().st_mtime) / 3600.0)


def _is_cache_stale() -> bool:
    age = _cache_age_hours()
    return age is None or age >= _max_age_hours()


def _download_instruments() -> List[Dict[str, Any]]:
    resp = requests.get(INSTRUMENTS_URL, timeout=60)
    resp.raise_for_status()
    instruments = resp.json()
    INSTRUMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_PATH.write_text(json.dumps(instruments), encoding="utf-8")
    logger.info("Instrument master refreshed rows=%s path=%s", len(instruments), INSTRUMENTS_PATH)
    return instruments


def invalidate_instrument_cache() -> None:
    global _INSTRUMENT_CACHE
    _INSTRUMENT_CACHE = None


def load_instruments(*, force: bool = False) -> List[Dict[str, Any]]:
    global _INSTRUMENT_CACHE
    stale = force or _is_cache_stale()
    if _INSTRUMENT_CACHE is not None and not stale:
        return _INSTRUMENT_CACHE

    if stale:
        try:
            _INSTRUMENT_CACHE = _download_instruments()
            return _INSTRUMENT_CACHE
        except Exception:
            logger.exception("Instrument master refresh failed; falling back to cached file")
            if INSTRUMENTS_PATH.exists():
                try:
                    _INSTRUMENT_CACHE = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))
                    return _INSTRUMENT_CACHE
                except Exception:
                    logger.exception("Instrument master cache read failed")

    if INSTRUMENTS_PATH.exists():
        try:
            _INSTRUMENT_CACHE = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))
            return _INSTRUMENT_CACHE
        except Exception:
            pass

    _INSTRUMENT_CACHE = _download_instruments()
    return _INSTRUMENT_CACHE
