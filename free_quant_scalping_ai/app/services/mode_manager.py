from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.services.market_calendar import get_market_status

PROFILES_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"

# Safety: only these non-sensitive keys are auto-managed.
SAFE_KEYS = {
    "ACTIVE_SYMBOLS",
    "SIGNAL_FILTER_LOG",
    "SIGNAL_RECORD_EXTENDED",
    "ENABLE_HERO_ZERO",
    "ANGEL_NFO_ENABLE_OI",
    "SLOW_ENGINE_LOOP_SEC",
    "SENSEX_SESSION_ENABLED",
    "SYSTEM_WARMUP_SEC",
    "ENTRY_MIN_TICKS",
    "RESTART_GUARD_SEC",
    "RESTART_CONF_DELTA",
}


def detect_mode(symbol: str = "NIFTY") -> str:
    ms = get_market_status(symbol)
    if bool(ms.get("is_open")):
        if bool(ms.get("is_expiry")):
            return "expiry"
        return "live"
    return "analysis"


def _profile_path(profile_name: str) -> Path:
    p = PROFILES_DIR / f"{profile_name}.json"
    if p.is_file():
        return p
    # optional expiry profile fallback
    return PROFILES_DIR / "live.json"


def apply_profile(profile_name: str) -> Dict[str, Any]:
    path = _profile_path(profile_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    applied: Dict[str, str] = {}
    skipped: Dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip().upper()
        if key not in SAFE_KEYS:
            skipped[key] = "unsafe_key_not_allowed"
            continue
        val = str(v)
        prev = os.getenv(key)
        if prev != val:
            os.environ[key] = val
        applied[key] = val

    return {
        "profile": profile_name,
        "resolved_profile_file": str(path),
        "applied": applied,
        "skipped": skipped,
        "applied_count": len(applied),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
