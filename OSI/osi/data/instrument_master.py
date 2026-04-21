from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENTS_PATH = Path(__file__).resolve().parents[2] / "storage" / "angel_instruments.json"

_INSTRUMENT_CACHE: Optional[List[Dict[str, Any]]] = None


def _download_instruments() -> List[Dict[str, Any]]:
    resp = requests.get(INSTRUMENTS_URL, timeout=20)
    resp.raise_for_status()
    instruments = resp.json()
    INSTRUMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_PATH.write_text(json.dumps(instruments), encoding="utf-8")
    return instruments


def load_instruments() -> List[Dict[str, Any]]:
    global _INSTRUMENT_CACHE
    if _INSTRUMENT_CACHE is not None:
        return _INSTRUMENT_CACHE
    if INSTRUMENTS_PATH.exists():
        try:
            _INSTRUMENT_CACHE = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))
            return _INSTRUMENT_CACHE
        except Exception:
            pass
    _INSTRUMENT_CACHE = _download_instruments()
    return _INSTRUMENT_CACHE

