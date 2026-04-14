"""
Process-level guards: warmup, minimum ticks after boot, post-restart confidence,
optional execution/price-history persistence. Does not change signal/scalping math.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_STATE_KEY_START = "_runtime_process_start_epoch"
_STATE_KEY_TICKS = "_runtime_symbol_ticks"
_STATE_KEY_LAST_PERSIST = "_runtime_last_persist_epoch"
_STATE_KEY_RESTART_PHASE_UNTIL = "_runtime_restart_phase_until"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


SYSTEM_WARMUP_SEC = _env_float("SYSTEM_WARMUP_SEC", 30.0)
ENTRY_MIN_TICKS = _env_int("ENTRY_MIN_TICKS", 8)
RESTART_GUARD_SEC = _env_float("RESTART_GUARD_SEC", 120.0)
RESTART_CONF_DELTA = _env_float("RESTART_CONF_DELTA", 7.0)
EXEC_STATE_PERSIST = os.getenv("EXEC_STATE_PERSIST", "0").strip().lower() in ("1", "true", "yes", "on")
EXEC_STATE_PERSIST_PATH = Path(os.getenv("EXEC_STATE_PERSIST_PATH", "logs/execution_runtime_state.json"))
EXEC_PERSIST_OPEN_ON_LOAD = os.getenv("EXEC_PERSIST_OPEN_ON_LOAD", "0").strip().lower() in ("1", "true", "yes", "on")
PERSIST_THROTTLE_SEC = _env_float("EXEC_STATE_PERSIST_THROTTLE_SEC", 10.0)


def mark_startup(state: Dict[str, Any]) -> None:
    """Call once at API startup (before live ticks)."""
    now = time.time()
    state[_STATE_KEY_START] = now
    state[_STATE_KEY_TICKS] = {}
    state[_STATE_KEY_LAST_PERSIST] = 0.0
    state[_STATE_KEY_RESTART_PHASE_UNTIL] = now + max(0.0, RESTART_GUARD_SEC)
    logger.info(
        "[RUNTIME] startup marked warmup=%ss min_ticks=%s restart_guard=%ss conf_delta=%.1f persist=%s",
        SYSTEM_WARMUP_SEC,
        ENTRY_MIN_TICKS,
        RESTART_GUARD_SEC,
        RESTART_CONF_DELTA,
        EXEC_STATE_PERSIST,
    )


def seconds_since_startup(state: Dict[str, Any]) -> float:
    t0 = float(state.get(_STATE_KEY_START) or 0.0)
    if t0 <= 0:
        return 0.0
    return max(0.0, time.time() - t0)


def record_successful_compute_tick(state: Dict[str, Any], symbol: str) -> int:
    """Increment per-symbol tick counter after a full live compute (not early-return paths)."""
    su = str(symbol or "").upper()
    bucket = state.setdefault(_STATE_KEY_TICKS, {})
    n = int(bucket.get(su) or 0) + 1
    bucket[su] = n
    return n


def tick_count(state: Dict[str, Any], symbol: str) -> int:
    su = str(symbol or "").upper()
    return int((state.get(_STATE_KEY_TICKS) or {}).get(su) or 0)


def stability_entry_skip_reason(
    state: Dict[str, Any],
    symbol: str,
    *,
    confidence: float,
    min_entry_confidence: float,
) -> Optional[str]:
    """
    Returns a skip reason consumed by ExecutionLifecycle as skip_entry_reason, or None.
    """
    su = str(symbol or "").upper()
    warm_left = SYSTEM_WARMUP_SEC - seconds_since_startup(state)
    if warm_left > 0:
        return f"system_warmup_{warm_left:.0f}s_remaining"

    n = tick_count(state, su)
    if n < ENTRY_MIN_TICKS:
        return f"min_ticks_{n}_lt_{ENTRY_MIN_TICKS}"

    until = float(state.get(_STATE_KEY_RESTART_PHASE_UNTIL) or 0.0)
    if time.time() < until:
        need = float(min_entry_confidence) + RESTART_CONF_DELTA
        if float(confidence) < need:
            return f"post_restart_conf_{float(confidence):.0f}_lt_{need:.0f}"

    return None


def merge_skip_reason(primary: Optional[str], secondary: Optional[str]) -> Optional[str]:
    if primary:
        return primary
    return secondary


def maybe_throttled_skip_log(
    state: Dict[str, Any],
    symbol: str,
    reason: Optional[str],
    *,
    interval_sec: float = 25.0,
) -> None:
    """INFO log stability skips (warmup / min ticks / post-restart) without spamming every tick."""
    if not reason:
        return
    r = str(reason)
    if not (
        r.startswith("system_warmup")
        or r.startswith("min_ticks_")
        or r.startswith("post_restart_conf_")
    ):
        return
    su = str(symbol or "").upper()
    bucket = state.setdefault("_runtime_skip_log_ts", {})
    if r.startswith("system_warmup"):
        key = f"{su}:warmup"
    elif r.startswith("min_ticks"):
        key = f"{su}:min_ticks"
    elif r.startswith("post_restart_conf"):
        key = f"{su}:post_restart"
    else:
        key = f"{su}:other"
    now = time.time()
    if now - float(bucket.get(key) or 0.0) < interval_sec:
        return
    bucket[key] = now
    logger.info("[RUNTIME] %s entry skipped: %s", su, r)


def load_persisted_execution_state(state: Dict[str, Any]) -> None:
    if not EXEC_STATE_PERSIST:
        return
    path = EXEC_STATE_PERSIST_PATH
    if not path.is_file():
        logger.info("[RUNTIME] persist load skipped (no file): %s", path)
        return
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[RUNTIME] persist load failed: %s", exc)
        return

    ex = data.get("_execution_trade")
    if isinstance(ex, dict):
        state["_execution_trade"] = ex
        if not EXEC_PERSIST_OPEN_ON_LOAD:
            for sym, row in list(ex.items()):
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "").upper() != "OPEN":
                    continue
                cleared = dict(row)
                cleared["status"] = "IDLE"
                cleared["last_exit_reason"] = row.get("last_exit_reason") or "persisted_open_cleared_on_load"
                ex[sym] = cleared
                logger.warning(
                    "[RUNTIME] persisted OPEN for %s demoted to IDLE (EXEC_PERSIST_OPEN_ON_LOAD=0)",
                    sym,
                )

    ph = data.get("_price_history")
    if isinstance(ph, dict):
        clean: Dict[str, List[float]] = {}
        for k, v in ph.items():
            if isinstance(v, list):
                try:
                    clean[str(k).upper()] = [float(x) for x in v[-128:]]
                except (TypeError, ValueError):
                    continue
        state["_price_history"] = clean

    ef = data.get("execution_final_signal")
    if isinstance(ef, dict):
        state["execution_final_signal"] = ef

    logger.info("[RUNTIME] restored persisted state from %s", path)


def maybe_persist_execution_state(state: Dict[str, Any]) -> None:
    if not EXEC_STATE_PERSIST:
        return
    now = time.time()
    last = float(state.get(_STATE_KEY_LAST_PERSIST) or 0.0)
    if now - last < PERSIST_THROTTLE_SEC:
        return
    state[_STATE_KEY_LAST_PERSIST] = now
    path = EXEC_STATE_PERSIST_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": time.time(),
            "_execution_trade": state.get("_execution_trade") or {},
            "_price_history": state.get("_price_history") or {},
            "execution_final_signal": state.get("execution_final_signal") or {},
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.debug("[RUNTIME] persist write failed: %s", exc)
