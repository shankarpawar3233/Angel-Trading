from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

PROFILES_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"

DEFAULT_STRATEGY: Dict[str, Any] = {
    "mode": "NORMAL",
    "target_multiplier": 1.0,
    "stop_multiplier": 1.0,
    "confidence_multiplier": 1.0,
    "aggregate_confidence_multiplier": 1.0,
}


def _load_profile(mode: str) -> Dict[str, Any]:
    p = PROFILES_DIR / f"{mode.lower()}.json"
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_strategy_config(market_intel: Dict[str, Any]) -> Dict[str, Any]:
    """
    Selects strategy config from intelligence:
      EXPIRY > VOLATILE > NORMAL
    Safe default: NORMAL.
    """
    mi = market_intel or {}
    mode = "NORMAL"
    if bool(mi.get("is_expiry")):
        mode = "EXPIRY"
    elif str(mi.get("market_type") or "").upper() == "VOLATILE":
        mode = "VOLATILE"

    base = dict(DEFAULT_STRATEGY)
    profile = _load_profile(mode)
    cfg = {**base, **profile}
    cfg["mode"] = str(cfg.get("mode") or mode).upper()
    for k in ("target_multiplier", "stop_multiplier", "confidence_multiplier", "aggregate_confidence_multiplier"):
        try:
            cfg[k] = float(cfg.get(k))
        except (TypeError, ValueError):
            cfg[k] = float(base[k])
    return cfg
