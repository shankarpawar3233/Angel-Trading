"""
Broker-facing execution hooks (stub). Enable with EXEC_BROKER_ENABLED=1.

Wire real SmartAPI / REST order placement here; keep idempotent where possible.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_SEC = 0.4


def broker_enabled() -> bool:
    return os.getenv("EXEC_BROKER_ENABLED", "").strip().lower() in ("1", "true", "yes")


def place_entry_order(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return broker order id or synthetic id. Retry on transient failures."""
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            # TODO: integrate SmartAPI placeOrder with payload strike/token/qty
            oid = f"SIM-{symbol}-{int(time.time() * 1000)}"
            logger.info("[EXEC_BROKER] place_entry %s %s", symbol, oid)
            return {"ok": True, "order_id": oid, "attempt": attempt + 1}
        except Exception as exc:
            last_err = exc
            time.sleep(_RETRY_DELAY_SEC)
    logger.warning("[EXEC_BROKER] place_entry failed %s: %s", symbol, last_err)
    return {"ok": False, "error": str(last_err)}


def place_exit_order(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            oid = f"SIM-EXIT-{symbol}-{int(time.time() * 1000)}"
            logger.info("[EXEC_BROKER] place_exit %s %s", symbol, oid)
            return {"ok": True, "order_id": oid, "attempt": attempt + 1}
        except Exception as exc:
            last_err = exc
            time.sleep(_RETRY_DELAY_SEC)
    logger.warning("[EXEC_BROKER] place_exit failed %s: %s", symbol, last_err)
    return {"ok": False, "error": str(last_err)}


def sync_position_from_broker(symbol: str) -> Optional[Dict[str, Any]]:
    """Optional: fetch open legs from broker and reconcile with local trade_state."""
    if not broker_enabled():
        return None
    # TODO: REST position book
    return None


def dispatch_execution_event(symbol: str, final_payload: Dict[str, Any]) -> None:
    if not broker_enabled():
        return
    st = str(final_payload.get("status") or "").upper()
    if st == "CONFIRMED":
        place_entry_order(symbol, final_payload)
    elif st == "EXIT":
        place_exit_order(symbol, final_payload)
