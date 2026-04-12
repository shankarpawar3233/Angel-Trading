"""
Telegram Bot API helper: optional env-based send, non-blocking wrapper, silent if unset.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = min(5.0, float(os.getenv("TELEGRAM_HTTP_TIMEOUT_SEC", "4.5")))


def _bot_token() -> str:
    """Bot API token: ``TELEGRAM_BOT_TOKEN``, or ``TELEGRAM_TOKEN`` if the former is unset."""
    t = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return t or os.getenv("TELEGRAM_TOKEN", "").strip()


def _chat_id() -> str:
    """Destination chat: ``TELEGRAM_CHAT_ID`` (numeric id, e.g. from @userinfobot)."""
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_configured() -> bool:
    if os.getenv("TELEGRAM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    return bool(_bot_token() and _chat_id())


def send_telegram_message(text: str, *, parse_mode: str = "Markdown") -> bool:
    """
    POST sendMessage. Returns True on 2xx response.
    No-op (returns False) if token or TELEGRAM_CHAT_ID missing.

    Token env: ``TELEGRAM_BOT_TOKEN`` (preferred) or ``TELEGRAM_TOKEN``.
    """
    token = _bot_token()
    chat_id = _chat_id()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    fields: Dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": str(text)[:4090],
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                j = json.loads(raw)
            except json.JSONDecodeError:
                return 200 <= (resp.status or 0) < 300
            return bool(j.get("ok", False))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
        except OSError:
            detail = ""
        logger.warning("Telegram send failed HTTP %s: %s %s", exc.code, exc.reason, detail or exc)
        return False
    except urllib.error.URLError as exc:
        logger.warning("Telegram send failed (network): %s", exc)
        return False
    except OSError as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def send_telegram_message_async(text: str, *, parse_mode: str = "Markdown") -> None:
    """Fire-and-forget; never raises."""

    def _run() -> None:
        try:
            send_telegram_message(text, parse_mode=parse_mode)
        except Exception as exc:
            logger.debug("Telegram async send: %s", exc)

    threading.Thread(target=_run, daemon=True, name="telegram-send").start()
