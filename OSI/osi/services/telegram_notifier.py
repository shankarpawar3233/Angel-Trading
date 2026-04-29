from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
import json
from typing import Callable, Dict, List, Optional
from urllib import error, parse, request

from osi.core.config import settings
from osi.core.models import SignalRecord

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        command_handler: Optional[Callable[[str, str, str], str]] = None,
        dashboard_summary_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.enabled = bool(settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id)
        self._chat_ids = self._resolve_chat_ids()
        self._queue: "queue.Queue[Dict[str, object]]" = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._command_handler = command_handler
        self._dashboard_summary_provider = dashboard_summary_provider
        self._updates_offset: int = 0
        if self.enabled:
            self._worker = threading.Thread(target=self._loop, name="osi-telegram-notifier", daemon=True)
            self._worker.start()
            logger.info("Telegram notifier enabled chat_count=%s", len(self._chat_ids))
        else:
            logger.info(
                "Telegram notifier disabled enabled_flag=%s token_present=%s chat_present=%s",
                settings.telegram_enabled,
                bool(settings.telegram_bot_token),
                bool(settings.telegram_chat_id),
            )

    def close(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        try:
            self._queue.put_nowait({"text": "__STOP__"})
        except queue.Full:
            pass

    def notify_signal_created(self, signal: SignalRecord, *, lead_engine: str = "") -> None:
        if not self.enabled:
            return
        msg = (
            f"OSI SIGNAL CREATED\n"
            f"ID: {signal.signal_id}\n"
            f"Symbol: {signal.symbol}\n"
            f"Signal: {signal.signal}\n"
            f"Strategy: {signal.strategy}\n"
            f"Lead Engine: {lead_engine or '-'}\n"
            f"Conf: {signal.confidence:.2f}\n"
            f"Entry: {signal.entry_price:.2f} | SL: {signal.stop_loss:.2f} | T2: {signal.target_2:.2f}\n"
            f"Option: {signal.option_symbol}"
        )
        self._enqueue(msg)
        if bool(settings.telegram_screenshot_on_signal_create):
            self._enqueue_photo(
                caption=f"OSI screenshot after signal create\n{signal.symbol} {signal.signal} {signal.signal_id}"
            )

    def notify_signal_closed(self, signal: SignalRecord) -> None:
        if not self.enabled:
            return
        msg = (
            f"OSI SIGNAL CLOSED\n"
            f"ID: {signal.signal_id}\n"
            f"Symbol: {signal.symbol}\n"
            f"Signal: {signal.signal}\n"
            f"Status: {signal.status.value}\n"
            f"Exit: {float(signal.exit_price or 0.0):.2f}\n"
            f"PnL: {float(signal.pnl or 0.0):.2f}"
        )
        self._enqueue(msg)
        if bool(settings.telegram_screenshot_on_signal_close):
            self._enqueue_photo(
                caption=f"OSI screenshot after signal close\n{signal.symbol} {signal.signal} {signal.signal_id}"
            )

    def notify_system_started(self, *, active_count: int) -> None:
        if not self.enabled:
            return
        msg = f"OSI SYSTEM STARTED\nActive trades loaded: {active_count}"
        summary = self._build_dashboard_summary()
        if summary:
            msg = f"{msg}\n\n{summary}"
        self._enqueue(msg)
        if bool(settings.telegram_screenshot_on_start):
            self._enqueue_photo(caption="OSI dashboard snapshot on startup")

    def notify_active_snapshot(self, signals: list[SignalRecord]) -> None:
        if not self.enabled:
            return
        if not signals:
            self._enqueue("OSI ACTIVE SNAPSHOT\nNo active trades.")
            return
        lines = ["OSI ACTIVE SNAPSHOT"]
        for s in signals[:10]:
            lines.append(
                f"{s.symbol} {s.signal} {s.strategy} | Entry {s.entry_price:.2f} | LTP {float(s.current_ltp or 0.0):.2f} | SL {s.stop_loss:.2f} | T2 {s.target_2:.2f}"
            )
        if len(signals) > 10:
            lines.append(f"... and {len(signals) - 10} more")
        self._enqueue("\n".join(lines))

    def notify_signal_update(self, signal: SignalRecord, *, event: str, price: float) -> None:
        if not self.enabled:
            return
        msg = (
            f"OSI SIGNAL UPDATE\n"
            f"ID: {signal.signal_id}\n"
            f"Symbol: {signal.symbol}\n"
            f"Event: {event}\n"
            f"Price: {float(price):.2f}\n"
            f"LTP: {float(signal.current_ltp or 0.0):.2f}"
        )
        self._enqueue(msg)
        if bool(settings.telegram_screenshot_on_signal_update):
            self._enqueue_photo(
                caption=f"OSI screenshot after update\n{signal.symbol} {signal.signal} {event} {signal.signal_id}"
            )

    def _enqueue(self, text: str) -> None:
        try:
            self._queue.put_nowait({"kind": "text", "text": text})
        except queue.Full:
            logger.warning("Telegram notifier queue full, dropping message")

    def _enqueue_photo(self, *, caption: str) -> None:
        try:
            self._queue.put_nowait({"kind": "photo", "caption": caption})
        except queue.Full:
            logger.warning("Telegram notifier queue full, dropping photo request")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._poll_commands()
                continue
            kind = str(item.get("kind") or "text")
            text = str(item.get("text") or "")
            if text == "__STOP__":
                break
            try:
                if kind == "photo":
                    self._send_dashboard_photo(caption=str(item.get("caption") or "OSI dashboard"))
                else:
                    self._send(text)
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)
            self._poll_commands()

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        for chat_id in self._chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if int(settings.telegram_thread_id or 0) > 0:
                payload["message_thread_id"] = int(settings.telegram_thread_id)
            data = parse.urlencode(payload).encode("utf-8")
            req = request.Request(url, data=data, method="POST")
            timeout = max(1.0, float(settings.telegram_timeout_sec))
            try:
                with request.urlopen(req, timeout=timeout) as resp:
                    _ = resp.read()
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="ignore")
                logger.warning("Telegram HTTP error status=%s body=%s", exc.code, body[:300])
            except Exception:
                raise

    def _send_dashboard_photo(self, *, caption: str) -> None:
        img = self._capture_dashboard_png()
        if img is None:
            self._send(f"{caption}\n(Screenshot skipped: playwright/chromium not available)")
            return
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
        for chat_id in self._chat_ids:
            fields = {
                "chat_id": str(chat_id),
                "caption": caption,
            }
            if int(settings.telegram_thread_id or 0) > 0:
                fields["message_thread_id"] = str(int(settings.telegram_thread_id))
            body, content_type = self._encode_multipart(fields, "photo", "osi_dashboard.png", img)
            req = request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", content_type)
            timeout = max(1.0, float(settings.telegram_timeout_sec) + 2.0)
            try:
                with request.urlopen(req, timeout=timeout) as resp:
                    _ = resp.read()
            except error.HTTPError as exc:
                body_txt = exc.read().decode("utf-8", errors="ignore")
                logger.warning("Telegram sendPhoto HTTP error status=%s body=%s", exc.code, body_txt[:300])

    def _capture_dashboard_png(self) -> bytes | None:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]
        except Exception:
            logger.warning("Playwright not available; cannot capture dashboard screenshot")
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    viewport={
                        "width": int(settings.telegram_screenshot_width),
                        "height": int(settings.telegram_screenshot_height),
                    }
                )
                page.goto(settings.telegram_dashboard_url, wait_until="networkidle", timeout=7000)
                time.sleep(0.25)
                img = page.screenshot(full_page=True, type="png")
                browser.close()
                return img
        except Exception as exc:
            logger.warning("Dashboard screenshot capture failed: %s", exc)
            return None

    def _poll_commands(self) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
        payload = {
            "timeout": 0,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._updates_offset > 0:
            payload["offset"] = str(self._updates_offset)
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        timeout = max(1.0, float(settings.telegram_timeout_sec))
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            decoded = json.loads(body)
        except Exception as exc:
            logger.debug("Telegram command poll failed: %s", exc)
            return
        if not isinstance(decoded, dict) or not decoded.get("ok"):
            return
        for upd in decoded.get("result", []):
            if not isinstance(upd, dict):
                continue
            upd_id = int(upd.get("update_id") or 0)
            self._updates_offset = max(self._updates_offset, upd_id + 1)
            self._handle_update(upd)

    def _handle_update(self, update: Dict[str, object]) -> None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat = msg.get("chat")
        chat_id = ""
        if isinstance(chat, dict):
            chat_id = str(chat.get("id") or "").strip()
        if chat_id and chat_id not in self._chat_ids:
            logger.info("Ignoring telegram command from unauthorized chat_id=%s", chat_id)
            return
        parts = text.split(maxsplit=1)
        command_token = (parts[0] or "").strip()
        command = command_token.split("@", 1)[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        self._execute_command(command, args, chat_id or str(settings.telegram_chat_id))

    def _execute_command(self, command: str, args: str, chat_id: str) -> None:
        if command == "/snapshot":
            self._send_dashboard_photo(caption="OSI snapshot via telegram command")
            summary = self._build_dashboard_summary()
            if summary:
                self._send_to_chat(chat_id, summary)
            self._send_to_chat(chat_id, "Snapshot sent.")
            return
        if command in {"/status", "/active", "/pause", "/resume"} and self._command_handler is not None:
            try:
                response = self._command_handler(command, args, chat_id)
            except Exception as exc:
                logger.warning("Telegram command handler failed command=%s err=%s", command, exc)
                response = f"Command failed: {command}"
            if response:
                self._send_to_chat(chat_id, response)
            return
        if command == "/help":
            self._send_to_chat(chat_id, "Commands: /status /active /pause /resume /snapshot")
            return
        self._send_to_chat(chat_id, "Unknown command. Try /help")

    def _build_dashboard_summary(self) -> str:
        if self._dashboard_summary_provider is None:
            return ""
        try:
            return str(self._dashboard_summary_provider() or "").strip()
        except Exception as exc:
            logger.warning("Dashboard summary provider failed: %s", exc)
            return ""

    def _send_to_chat(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": str(chat_id),
            "text": str(text),
            "disable_web_page_preview": True,
        }
        if int(settings.telegram_thread_id or 0) > 0:
            payload["message_thread_id"] = int(settings.telegram_thread_id)
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        timeout = max(1.0, float(settings.telegram_timeout_sec))
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                _ = resp.read()
        except Exception as exc:
            logger.warning("Telegram direct reply failed chat_id=%s err=%s", chat_id, exc)

    @staticmethod
    def _encode_multipart(
        fields: Dict[str, str], file_field: str, filename: str, file_content: bytes
    ) -> tuple[bytes, str]:
        boundary = f"----OSI{uuid.uuid4().hex}"
        lines: List[bytes] = []
        for k, v in fields.items():
            lines.append(f"--{boundary}\r\n".encode("utf-8"))
            lines.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
            lines.append(str(v).encode("utf-8"))
            lines.append(b"\r\n")
        lines.append(f"--{boundary}\r\n".encode("utf-8"))
        lines.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("utf-8")
        )
        lines.append(file_content)
        lines.append(b"\r\n")
        lines.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(lines)
        return body, f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _resolve_chat_ids() -> List[str]:
        out: List[str] = []
        base = str(settings.telegram_chat_id or "").strip()
        if base:
            out.append(base)
        extra = str(settings.telegram_subscribers or "").strip()
        if extra:
            for token in extra.split(","):
                t = token.strip()
                if t and t not in out:
                    out.append(t)
        return out
