from __future__ import annotations

import html as html_lib
import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error, parse, request

from osi.core.config import settings
from osi.core.market_phase import get_market_phase
from osi.core.models import SignalRecord, SignalStatus

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def _esc(value: Any) -> str:
    """HTML-escape arbitrary content for Telegram parse_mode=HTML."""
    return html_lib.escape("" if value is None else str(value), quote=False)


def _direction_emoji(signal_type: Any) -> str:
    s = str(signal_type or "").upper()
    if "CE" in s or "BUY" in s and "PE" not in s:
        return "📈"
    if "PE" in s:
        return "📉"
    return "•"


def _status_emoji(status: Any) -> str:
    s = str(status or "").upper()
    mapping = {
        "ACTIVE": "🟢",
        "CONFIRMED": "🟢",
        "CLOSED": "✅",
        "STOPPED": "❌",
        "TARGET_HIT": "🎯",
        "EXPIRED": "⏱",
        "CANCELLED": "🚫",
        "REJECTED": "🚫",
    }
    return mapping.get(s, "•")


def _now_ist_str() -> str:
    return datetime.now(_IST).strftime("%H:%M:%S IST")


def _fmt_held(start: Optional[datetime]) -> str:
    if start is None:
        return "-"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - start
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def _pnl_block(entry: float, ltp: float, qty: float = 1.0) -> Tuple[float, float, str]:
    """Return (pnl_points, pnl_pct, arrow_emoji)."""
    pts = round(float(ltp) - float(entry), 2)
    pct = round((pts / float(entry)) * 100.0, 2) if entry else 0.0
    if pts > 0:
        return pts, pct, "🟢"
    if pts < 0:
        return pts, pct, "🔴"
    return pts, pct, "⚪"


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
        # signal_id -> {"chats": {chat_id: message_id}, "last_text": str, "last_edit_at": float}
        self._trackers: Dict[str, Dict[str, Any]] = {}
        self._trackers_lock = threading.Lock()
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
            self._queue.put_nowait({"kind": "stop"})
        except queue.Full:
            pass

    # ---------------------------------------------------------------- public

    def notify_signal_created(self, signal: SignalRecord, *, lead_engine: str = "") -> None:
        if not self.enabled:
            return
        text = self._format_signal_created_html(signal, lead_engine=lead_engine)
        self._enqueue({"kind": "html", "text": text})
        if bool(settings.telegram_screenshot_on_signal_create):
            self._enqueue_photo(
                caption=f"OSI screenshot after signal create | {signal.symbol} {signal.signal} {signal.signal_id}"
            )
        # Bootstrap the live tracker. Sending happens in the worker thread.
        self._enqueue({"kind": "tracker_init", "signal": signal})

    def notify_signal_closed(self, signal: SignalRecord) -> None:
        if not self.enabled:
            return
        # Final-state edit on tracker first, then a fresh closing event message.
        self._enqueue({"kind": "tracker_finalize", "signal": signal})
        text = self._format_signal_closed_html(signal)
        self._enqueue({"kind": "html", "text": text})
        if bool(settings.telegram_screenshot_on_signal_close):
            self._enqueue_photo(
                caption=f"OSI screenshot after signal close | {signal.symbol} {signal.signal} {signal.signal_id}"
            )

    def notify_signal_update(self, signal: SignalRecord, *, event: str, price: float) -> None:
        if not self.enabled:
            return
        text = self._format_signal_update_html(signal, event=event, price=price)
        self._enqueue({"kind": "html", "text": text})
        # Refresh tracker promptly on real lifecycle events.
        self._enqueue({"kind": "tracker_update", "signal": signal})
        if bool(settings.telegram_screenshot_on_signal_update):
            self._enqueue_photo(
                caption=f"OSI screenshot after update | {signal.symbol} {signal.signal} {event} {signal.signal_id}"
            )

    def notify_system_started(self, *, active_count: int) -> None:
        if not self.enabled:
            return
        body = (
            f"🚀 <b>OSI SYSTEM STARTED</b>\n"
            f"Active trades loaded: <b>{int(active_count)}</b>\n"
            f"<i>{_now_ist_str()}</i>"
        )
        summary = self._build_dashboard_summary()
        if summary:
            # Provider already returns Telegram-safe HTML; do not double-escape.
            body = f"{body}\n\n{summary}"
        self._enqueue({"kind": "html", "text": body})
        if bool(settings.telegram_screenshot_on_start):
            self._enqueue_photo(caption=f"OSI dashboard snapshot on startup · {_now_ist_str()}")

    def notify_system_stopped(self, *, reason: str = "shutdown") -> None:
        if not self.enabled:
            return
        text = (
            f"🛑 <b>OSI SYSTEM STOPPED</b>\n"
            f"Reason: <i>{_esc(reason)}</i>\n"
            f"<i>{_now_ist_str()}</i>"
        )
        try:
            self._send(text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Telegram stop notification failed: %s", exc)

    def notify_active_snapshot(self, signals: List[SignalRecord]) -> None:
        if not self.enabled:
            return
        if not signals:
            self._enqueue({"kind": "html", "text": "📭 <b>OSI ACTIVE SNAPSHOT</b>\nNo active trades."})
            return
        lines = ["📊 <b>OSI ACTIVE SNAPSHOT</b>"]
        for s in signals[:10]:
            ltp = float(s.current_ltp or 0.0)
            pts, pct, arrow = _pnl_block(float(s.entry_price or 0.0), ltp)
            lines.append(
                f"{_direction_emoji(s.signal)} <b>{_esc(s.symbol)}</b> "
                f"{_esc(s.signal)} · {_esc(s.strategy)}\n"
                f"   Entry <code>{float(s.entry_price):.2f}</code> · "
                f"LTP <code>{ltp:.2f}</code> {arrow} {pct:+.2f}%\n"
                f"   SL <code>{float(s.stop_loss):.2f}</code> · "
                f"T2 <code>{float(s.target_2):.2f}</code>"
            )
        if len(signals) > 10:
            lines.append(f"... and <b>{len(signals) - 10}</b> more")
        self._enqueue({"kind": "html", "text": "\n".join(lines)})

    def update_signal_live(self, signal: SignalRecord) -> None:
        """Enqueue a tracker refresh for the given signal. Safe to call often."""
        if not self.enabled:
            return
        self._enqueue({"kind": "tracker_update", "signal": signal})

    def untrack_signal(self, signal_id: str) -> None:
        """Drop tracker bookkeeping for a closed signal."""
        with self._trackers_lock:
            self._trackers.pop(str(signal_id), None)

    # --------------------------------------------------------------- queueing

    def _enqueue(self, item: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            logger.warning("Telegram notifier queue full, dropping item kind=%s", item.get("kind"))

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
            kind = str(item.get("kind") or "html")
            try:
                if kind == "stop":
                    break
                if kind == "photo":
                    self._send_dashboard_photo(caption=str(item.get("caption") or "OSI dashboard"))
                elif kind == "html":
                    self._send(str(item.get("text") or ""), parse_mode="HTML")
                elif kind == "text":
                    self._send(str(item.get("text") or ""))
                elif kind == "tracker_init":
                    sig = item.get("signal")
                    if isinstance(sig, SignalRecord):
                        self._tracker_send_or_edit(sig, finalize=False)
                elif kind == "tracker_update":
                    sig = item.get("signal")
                    if isinstance(sig, SignalRecord):
                        self._tracker_send_or_edit(sig, finalize=False)
                elif kind == "tracker_finalize":
                    sig = item.get("signal")
                    if isinstance(sig, SignalRecord):
                        self._tracker_send_or_edit(sig, finalize=True)
                else:
                    self._send(str(item.get("text") or ""))
            except Exception as exc:
                logger.warning("Telegram send failed kind=%s err=%s", kind, exc)
            self._poll_commands()

    # ------------------------------------------------------------- formatters

    def _format_signal_created_html(self, s: SignalRecord, *, lead_engine: str) -> str:
        rr = self._risk_reward(s)
        phase = get_market_phase(datetime.now(_IST))
        return (
            f"🆕 <b>OSI SIGNAL</b> {_direction_emoji(s.signal)} <b>{_esc(s.signal)}</b>\n"
            f"<b>{_esc(s.symbol)}</b> · {_esc(s.strategy)}"
            f"{' · phase ' + _esc(phase) if phase else ''}\n"
            f"Option: <code>{_esc(s.option_symbol)}</code>\n"
            f"\n"
            f"Entry <code>{float(s.entry_price):.2f}</code> · "
            f"SL <code>{float(s.stop_loss):.2f}</code> · "
            f"T1 <code>{float(s.target_1):.2f}</code>\n"
            f"T2 <code>{float(s.target_2):.2f}</code>"
            f"{(' · T3 <code>' + format(float(s.target_3 or 0.0), '.2f') + '</code>') if s.target_3 else ''}\n"
            f"R:R <b>{rr:.2f}</b> · Conf <b>{float(s.confidence):.0f}</b>\n"
            f"\n"
            f"Lead: <i>{_esc(lead_engine or s.trigger_engine or '-')}</i>\n"
            f"ID: <code>{_esc(s.signal_id)}</code>\n"
            f"<i>{_now_ist_str()}</i>"
        )

    def _format_signal_update_html(self, s: SignalRecord, *, event: str, price: float) -> str:
        ltp = float(s.current_ltp or price)
        pts, pct, arrow = _pnl_block(float(s.entry_price), ltp)
        return (
            f"🔔 <b>OSI UPDATE</b> · <i>{_esc(event)}</i>\n"
            f"{_direction_emoji(s.signal)} <b>{_esc(s.symbol)}</b> {_esc(s.signal)}\n"
            f"Trigger price: <code>{float(price):.2f}</code>\n"
            f"LTP <code>{ltp:.2f}</code>  {arrow} {pct:+.2f}%  ({pts:+.2f} pts)\n"
            f"SL <code>{float(s.stop_loss):.2f}</code> · "
            f"T2 <code>{float(s.target_2):.2f}</code>\n"
            f"ID: <code>{_esc(s.signal_id)}</code>"
        )

    def _format_signal_closed_html(self, s: SignalRecord) -> str:
        exit_price = float(s.exit_price or s.current_ltp or s.entry_price)
        pts, pct, arrow = _pnl_block(float(s.entry_price), exit_price)
        emoji = _status_emoji(s.status.value if isinstance(s.status, SignalStatus) else s.status)
        return (
            f"{emoji} <b>OSI SIGNAL CLOSED</b>\n"
            f"{_direction_emoji(s.signal)} <b>{_esc(s.symbol)}</b> {_esc(s.signal)} · "
            f"{_esc(s.strategy)}\n"
            f"Status: <b>{_esc(s.status.value if isinstance(s.status, SignalStatus) else s.status)}</b>\n"
            f"\n"
            f"Entry <code>{float(s.entry_price):.2f}</code> → "
            f"Exit <code>{exit_price:.2f}</code>\n"
            f"P&amp;L  {arrow} <b>{pts:+.2f} pts</b> ({pct:+.2f}%) · ₹{float(s.pnl or 0.0):+.2f}\n"
            f"Held: {_fmt_held(s.entry_time)}\n"
            f"ID: <code>{_esc(s.signal_id)}</code>"
        )

    def _format_tracker_html(self, s: SignalRecord, *, finalize: bool = False) -> str:
        ref_price = float(s.exit_price) if (finalize and s.exit_price) else float(s.current_ltp or s.entry_price)
        pts, pct, arrow = _pnl_block(float(s.entry_price), ref_price)
        sl = float(s.stop_loss)
        t1 = float(s.target_1)
        t2 = float(s.target_2)
        t3 = float(s.target_3) if s.target_3 else 0.0

        def _check(level: float) -> str:
            if level <= 0:
                return "—"
            if ref_price >= level:
                return "✅"
            dist_pct = ((level - ref_price) / ref_price * 100.0) if ref_price else 0.0
            return f"{dist_pct:+.1f}%"

        def _sl_dist() -> str:
            if not ref_price:
                return "—"
            dist_pct = ((sl - ref_price) / ref_price * 100.0)
            return f"{dist_pct:+.1f}%"

        phase = get_market_phase(datetime.now(_IST))

        status_val = s.status.value if isinstance(s.status, SignalStatus) else str(s.status)
        header_emoji = _status_emoji(status_val) if finalize else "📡"
        header_text = "OSI TRADE CLOSED" if finalize else "OSI LIVE TRACKER"
        partial = " · partial-booked" if s.partial_booked else ""

        price_label = "Exit  " if (finalize and s.exit_price) else "LTP   "
        lines = [
            f"{header_emoji} <b>{header_text}</b>",
            f"{_direction_emoji(s.signal)} <b>{_esc(s.symbol)}</b> "
            f"{_esc(s.signal)} · {_esc(s.strategy)}{_esc(partial)}"
            + (f" · phase {_esc(phase)}" if phase else ""),
            f"Option: <code>{_esc(s.option_symbol)}</code>",
            "",
            f"Entry  <code>{float(s.entry_price):.2f}</code>",
            f"{price_label} <code>{ref_price:.2f}</code>  {arrow} {pct:+.2f}%  ({pts:+.2f} pts)",
            f"SL     <code>{sl:.2f}</code>  ({_sl_dist()})",
            f"T1     <code>{t1:.2f}</code>  {_check(t1)}",
            f"T2     <code>{t2:.2f}</code>  {_check(t2)}",
        ]
        if t3 > 0:
            lines.append(f"T3     <code>{t3:.2f}</code>  {_check(t3)}")
        if finalize and s.pnl is not None:
            lines.append(f"P&amp;L    <b>₹{float(s.pnl):+.2f}</b>")
        lines.extend(
            [
                "",
                f"Held   {_fmt_held(s.entry_time)} · "
                f"Conf <b>{float(s.confidence):.0f}</b> · "
                f"Status <b>{_esc(status_val)}</b>",
                f"ID: <code>{_esc(s.signal_id)}</code>",
                f"<i>Updated: {_now_ist_str()}</i>",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _risk_reward(s: SignalRecord) -> float:
        risk = float(s.entry_price) - float(s.stop_loss)
        reward = float(s.target_2 or s.target_1 or 0.0) - float(s.entry_price)
        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)

    # ------------------------------------------------------- tracker plumbing

    def _tracker_send_or_edit(self, signal: SignalRecord, *, finalize: bool) -> None:
        if not self._chat_ids:
            return
        text = self._format_tracker_html(signal, finalize=finalize)
        sid = str(signal.signal_id)
        with self._trackers_lock:
            entry = self._trackers.get(sid)

        if entry is None:
            chat_messages = self._send_html_with_ids(text)
            if not chat_messages:
                return
            with self._trackers_lock:
                self._trackers[sid] = {
                    "chats": chat_messages,
                    "last_text": text,
                    "last_edit_at": time.time(),
                }
            return

        last_text = str(entry.get("last_text") or "")
        if not finalize and last_text == text:
            return
        chats: Dict[str, int] = dict(entry.get("chats") or {})
        for chat_id, msg_id in chats.items():
            self._edit_html(chat_id=chat_id, message_id=int(msg_id), text=text)
        with self._trackers_lock:
            cur = self._trackers.get(sid)
            if cur is not None:
                cur["last_text"] = text
                cur["last_edit_at"] = time.time()

    def _send_html_with_ids(self, text: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        for chat_id in self._chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if int(settings.telegram_thread_id or 0) > 0:
                payload["message_thread_id"] = int(settings.telegram_thread_id)
            data = parse.urlencode(payload).encode("utf-8")
            req = request.Request(url, data=data, method="POST")
            timeout = max(1.0, float(settings.telegram_timeout_sec))
            try:
                with request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                decoded = json.loads(body)
                if isinstance(decoded, dict) and decoded.get("ok"):
                    result = decoded.get("result") or {}
                    msg_id = int(result.get("message_id") or 0)
                    if msg_id:
                        out[str(chat_id)] = msg_id
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="ignore")
                logger.warning("Telegram tracker create HTTP error status=%s body=%s", exc.code, body[:300])
            except Exception as exc:
                logger.warning("Telegram tracker create failed chat=%s err=%s", chat_id, exc)
        return out

    def _edit_html(self, *, chat_id: str, message_id: int, text: str) -> None:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/editMessageText"
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        timeout = max(1.0, float(settings.telegram_timeout_sec))
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                _ = resp.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            # "message is not modified" is benign and silenced.
            if "message is not modified" in body.lower():
                return
            logger.warning("Telegram tracker edit HTTP error status=%s body=%s", exc.code, body[:300])
        except Exception as exc:
            logger.warning("Telegram tracker edit failed chat=%s msg=%s err=%s", chat_id, message_id, exc)

    # --------------------------------------------------------------- send/raw

    def _send(self, text: str, *, parse_mode: Optional[str] = None) -> None:
        if not text:
            return
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        for chat_id in self._chat_ids:
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
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
            base = str(settings.telegram_dashboard_url or "").strip()
            sep = "&" if "?" in base else "?"
            # Cache-buster so any intermediate proxy / browser cache cannot serve stale data.
            url = f"{base}{sep}_={int(time.time() * 1000)}"
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={
                        "width": int(settings.telegram_screenshot_width),
                        "height": int(settings.telegram_screenshot_height),
                    }
                )
                # Disable HTTP cache so AJAX hits the live API every time.
                try:
                    context.set_extra_http_headers(
                        {"Cache-Control": "no-cache", "Pragma": "no-cache"}
                    )
                except Exception:
                    pass
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=10000)
                # Allow JS to fetch /signals, /signals/active, /metrics, etc.
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                time.sleep(1.0)
                img = page.screenshot(full_page=True, type="png")
                context.close()
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
            # 1) Live state summary first - quick, always available, HTML-formatted.
            summary = self._build_dashboard_summary()
            if summary:
                self._send_to_chat(chat_id, summary, parse_mode="HTML")
            else:
                self._send_to_chat(chat_id, "📡 <b>OSI SNAPSHOT</b>\n(no summary available)", parse_mode="HTML")
            # 2) Latest dashboard screenshot (slow, ~2-4s via Playwright).
            self._send_dashboard_photo(
                caption=f"OSI dashboard · captured {_now_ist_str()}"
            )
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
            self._send_to_chat(
                chat_id,
                "Commands: /status /active /pause /resume /snapshot",
            )
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

    def _send_to_chat(self, chat_id: str, text: str, *, parse_mode: Optional[str] = None) -> None:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": str(text),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
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
