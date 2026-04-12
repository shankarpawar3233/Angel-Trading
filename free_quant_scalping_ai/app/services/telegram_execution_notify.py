"""
Deduped Telegram notifications for ``execution_final_signal`` updates.

Called after ExecutionLifecycle mutates ``state["execution_final_signal"]``.
Dedupe state lives under ``state["_last_telegram_signal"]`` (per symbol).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from app.services import telegram_formatter
from app.services.telegram_service import (
    send_telegram_message,
    send_telegram_message_async,
    telegram_configured,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_BUCKET_KEY = "_last_telegram_signal"
_HOLD_PNL_STEP = float(os.getenv("TELEGRAM_HOLD_PNL_STEP", "5"))
_HOLD_MIN_SEC = float(os.getenv("TELEGRAM_HOLD_MIN_INTERVAL_SEC", "120"))


def _bucket(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.setdefault(_BUCKET_KEY, {})


def _fingerprint_entry_exit(payload: Dict[str, Any]) -> str:
    st = str(payload.get("status") or "")
    stage = str(payload.get("stage") or "")
    reason = str(payload.get("reason") or "")
    sig = str(payload.get("signal") or "")
    ent = str(payload.get("entry") or "")
    ltp = str(payload.get("ltp") or "")
    pnl = str(payload.get("pnl") or "")
    return f"{st}|{stage}|{reason}|{sig}|{ent}|{ltp}|{pnl}"


def on_final_signal_updated(state: Dict[str, Any], symbol: str, payload: Optional[Dict[str, Any]]) -> None:
    """
    Dispatch Telegram for ENTRY / EXIT / throttled HOLD. Never raises.
    Skips NO_TRADE by default (set TELEGRAM_NOTIFY_NO_TRADE=1 to enable).
    """
    if not telegram_configured() or not isinstance(payload, dict) or not payload:
        return
    sym = str(symbol or "").upper()
    st = str(payload.get("status") or "").upper()
    stage = str(payload.get("stage") or "").upper()

    if st == "NO_TRADE" and os.getenv("TELEGRAM_NOTIFY_NO_TRADE", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return

    b = _bucket(state)
    prev = b.get(sym) or {}

    if st == "EXIT":
        fp = _fingerprint_entry_exit(payload)
        if prev.get("fp") == fp:
            return
        try:
            text = telegram_formatter.format_exit(payload)
            send_telegram_message_async(text, parse_mode="HTML")
        except Exception as exc:
            logger.debug("Telegram EXIT notify: %s", exc)
        b[sym] = {**prev, "fp": fp, "hold_last_pnl": None, "hold_last_ts": time.time()}
        return

    if st == "CONFIRMED" and stage == "ENTRY":
        fp = _fingerprint_entry_exit(payload)
        if prev.get("fp") == fp:
            return
        try:
            text = telegram_formatter.format_entry(payload)
            send_telegram_message_async(text, parse_mode="HTML")
        except Exception as exc:
            logger.debug("Telegram ENTRY notify: %s", exc)
        now = time.time()
        b[sym] = {**prev, "fp": fp, "hold_last_pnl": None, "hold_last_ts": now}
        return

    if st == "CONFIRMED" and stage == "HOLD":
        try:
            pnl = float(payload.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        last_pnl = prev.get("hold_last_pnl")
        last_ts = float(prev.get("hold_last_ts") or 0.0)
        now = time.time()
        if now - last_ts < _HOLD_MIN_SEC:
            return
        if last_pnl is not None and abs(pnl - float(last_pnl)) < _HOLD_PNL_STEP:
            return
        try:
            text = telegram_formatter.format_hold(payload)
            send_telegram_message_async(text, parse_mode="HTML")
        except Exception as exc:
            logger.debug("Telegram HOLD notify: %s", exc)
        b[sym] = {**prev, "hold_last_pnl": pnl, "hold_last_ts": now}
        return


def send_startup_telegram_sync() -> bool:
    """
    Send the startup line to Telegram synchronously (blocks briefly on HTTP).

    Use from FastAPI startup via ``await loop.run_in_executor(None, send_startup_telegram_sync)``
    so the message is actually delivered before the server finishes booting.
    """
    if not telegram_configured():
        if os.getenv("TELEGRAM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
            logger.info("Telegram: disabled (TELEGRAM_ENABLED=0)")
        else:
            logger.info(
                "Telegram: not configured — set TELEGRAM_CHAT_ID and a bot token "
                "(TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN), e.g. in .env at project root, then restart"
            )
        return False
    try:
        text = telegram_formatter.format_system("🚀 Trading system started successfully")
        ok = send_telegram_message(text, parse_mode="HTML")
        if ok:
            logger.info("Telegram: startup message sent to bot")
        else:
            logger.warning(
                "Telegram: startup message not accepted (check token, chat id, and that you "
                "pressed /start in the bot chat)"
            )
        return bool(ok)
    except Exception as exc:
        logger.warning("Telegram startup notify error: %s", exc)
        return False


def schedule_startup_telegram() -> None:
    """Synchronous startup ping; prefer awaiting ``send_startup_telegram_sync`` from async startup."""
    send_startup_telegram_sync()
