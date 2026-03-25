"""Per-engine flags and thresholds from environment (no .env file edits required)."""

from __future__ import annotations

import os


def _truthy(val: str | None, default: str = "1") -> bool:
    return (val or default).strip().lower() in ("1", "true", "yes", "on")


def engine_enabled(name: str) -> bool:
    return _truthy(os.getenv(f"ENGINE_{name.upper()}_ENABLED", "1"), "1")


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default
