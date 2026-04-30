from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import pyotp

from osi.core.config import settings
from osi.core.models import MarketTick
from osi.data.instrument_master import load_instruments
from osi.services.expiry_engine import ExpiryEngine

logger = logging.getLogger(__name__)


TickHandler = Callable[[MarketTick], Awaitable[None]]


class AngelWebSocketSource:
    def __init__(self) -> None:
        self.expiry_engine = ExpiryEngine()
        self._enabled_symbols: List[str] = ["NIFTY"] + (["SENSEX"] if bool(settings.enable_sensex) else [])
        self._handler: Optional[TickHandler] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: asyncio.Task | None = None
        self._bridge_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._ws = None
        self._stream_thread: Optional[threading.Thread] = None

        self._instrument_rows: List[Dict[str, Any]] = []
        self._index_tokens: Dict[str, Tuple[int, str]] = {}  # symbol -> (exchangeType, token)
        self._option_meta_by_token: Dict[str, Dict[str, Any]] = {}
        self._subscribed_option_tokens: Dict[str, Tuple[int, str]] = {}
        self._desired_option_tokens_by_symbol: Dict[str, Dict[str, Tuple[int, str]]] = {
            sym: {} for sym in self._enabled_symbols
        }
        self._current_atm: Dict[str, float] = {}
        self._index_prices: Dict[str, float] = {}
        # symbol -> expiry(YYYYMMDD) -> strike -> leg -> fields
        self._option_chain: Dict[str, Dict[str, Dict[str, Dict[str, Dict[str, float]]]]] = {
            sym: {} for sym in self._enabled_symbols
        }
        self._last_index_emit: Dict[str, float] = {}
        self._raw_tick_count: int = 0
        self._last_tick_log_ts: float = 0.0
        self._emit_skip_log_ts: Dict[str, float] = {}
        self._last_any_tick_ts: float = 0.0
        self._last_exchange_ts_by_symbol: Dict[str, datetime] = {}
        self._bridge_queue: queue.Queue[MarketTick] = queue.Queue(maxsize=20000)
        self._lock = threading.RLock()
        self._auth_payload: Optional[Dict[str, str]] = None
        self._dropped_payload_count: int = 0
        self._last_dropped_payload_log_ts: float = 0.0
        self._awaiting_first_tick_after_reconnect: bool = False
        self._reconnect_open_ts: float = 0.0

    async def start(self, handler: TickHandler) -> None:
        self._running = True
        self._handler = handler
        self._loop = asyncio.get_running_loop()
        self._instrument_rows = load_instruments()
        self._index_tokens = self._build_index_tokens()
        self._bridge_task = asyncio.create_task(self._bridge_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._task = asyncio.create_task(self._run_smartapi_stream())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._bridge_task:
            self._bridge_task.cancel()
            try:
                await self._bridge_task
            except asyncio.CancelledError:
                pass
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None and hasattr(self._ws, "close_connection"):
            try:
                self._ws.close_connection()
            except Exception:
                pass

    async def _bridge_loop(self) -> None:
        while self._running:
            try:
                tick = await asyncio.to_thread(self._bridge_queue.get, True, 0.5)
            except queue.Empty:
                continue
            try:
                if self._handler is not None:
                    await self._handler(tick)
            except Exception:
                logger.exception("Tick bridge handler failure")

    async def _run_smartapi_stream(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._auth_payload = await asyncio.to_thread(self._create_or_refresh_session)
                await asyncio.to_thread(self._run_ws_blocking)
                backoff = 1.0
            except Exception as exc:
                logger.exception("SmartWebSocketV2 loop failed: %s", exc)
            sleep_for = min(float(settings.ws_reconnect_max_backoff_sec), backoff)
            sleep_for = sleep_for + random.uniform(0.0, 0.5)
            await asyncio.sleep(sleep_for)
            backoff = min(float(settings.ws_reconnect_max_backoff_sec), backoff * 2.0)

    def _create_or_refresh_session(self) -> Dict[str, str]:
        from SmartApi import SmartConnect  # type: ignore[import]

        api_key = settings.angel_api_key.strip() or str(os.getenv("ANGEL_API_KEY", "")).strip()
        client_id = settings.angel_client_id.strip() or str(os.getenv("ANGEL_CLIENT_ID", "")).strip()
        password = settings.angel_password.strip() or str(os.getenv("ANGEL_PASSWORD", "")).strip()
        feed_token = settings.angel_feed_token.strip() or str(os.getenv("ANGEL_FEED_TOKEN", "")).strip()
        if not all([api_key, client_id, password]):
            raise RuntimeError("Missing Angel credentials (set OSI_ANGEL_* or ANGEL_*)")

        smart = SmartConnect(api_key=api_key)
        totp_secret = settings.angel_totp_secret.strip() or str(os.getenv("ANGEL_TOTP_SECRET", "")).strip()
        if totp_secret:
            otp = pyotp.TOTP(totp_secret).now()
            session = smart.generateSession(client_id, password, otp)
        else:
            session = smart.generateSession(client_id, password)
        data = (session or {}).get("data") or {}
        auth_token = data.get("jwtToken") or data.get("authToken")
        if not auth_token:
            raise RuntimeError(f"Angel session missing auth token: {session}")
        if not feed_token:
            feed_token = str(smart.getfeedToken())
        return {
            "auth_token": str(auth_token),
            "api_key": api_key,
            "client_code": client_id,
            "feed_token": str(feed_token),
        }

    def _run_ws_blocking(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore[import]

        if not self._auth_payload:
            raise RuntimeError("Auth payload unavailable")

        payload = self._auth_payload
        self._ws = SmartWebSocketV2(
            payload["auth_token"],
            payload["api_key"],
            payload["client_code"],
            payload["feed_token"],
        )
        self._ws.on_open = self._on_open
        self._ws.on_data = self._on_data
        self._ws.on_error = self._on_error
        self._ws.on_close = self._on_close
        self._ws.connect()

    def _on_open(self, _wsapp: Any) -> None:
        logger.info("SmartWebSocketV2 on_open received, subscribing index tokens")
        self._awaiting_first_tick_after_reconnect = True
        self._reconnect_open_ts = time.time()
        self._subscribe_index_tokens()
        self._resubscribe_option_tokens()

    def _on_error(self, _wsapp: Any, error: Any) -> None:
        logger.error("SmartWebSocketV2 error: %s", error)
        try:
            if self._ws is not None and hasattr(self._ws, "close_connection"):
                self._ws.close_connection()
        except Exception:
            pass

    def _on_close(self, _wsapp: Any, *args: Any, **kwargs: Any) -> None:
        logger.warning("SmartWebSocketV2 disconnected args=%s kwargs=%s", args, kwargs)

    def _on_data(self, _wsapp: Any, message: Any) -> None:
        rows = self._extract_rows(message)
        if not rows:
            self._dropped_payload_count += 1
            now = time.time()
            if (now - self._last_dropped_payload_log_ts) >= 5.0:
                self._last_dropped_payload_log_ts = now
                logger.warning("Dropped websocket payload(s) parse_empty total_dropped=%s", self._dropped_payload_count)
            return
        now = time.time()
        self._last_any_tick_ts = now
        if self._awaiting_first_tick_after_reconnect and self._reconnect_open_ts > 0:
            reconnect_ms = (now - self._reconnect_open_ts) * 1000.0
            self._awaiting_first_tick_after_reconnect = False
            logger.info("Reconnect-to-first-tick latency_ms=%.2f", reconnect_ms)
        self._raw_tick_count += len(rows)
        if rows and (now - self._last_tick_log_ts) >= 5.0:
            self._last_tick_log_ts = now
            sample = rows[0] if rows else {}
            logger.info(
                "Tick callback rows=%s total_rows=%s sample_keys=%s",
                len(rows),
                self._raw_tick_count,
                sorted(list(sample.keys()))[:10],
            )
        for row in rows:
            norm = self._normalize_tick(row)
            token = norm.get("token")
            if not token:
                continue
            ltp = norm.get("ltp")
            if ltp is None:
                continue
            exch_ts = norm.get("exchange_timestamp")

            symbol = self._index_symbol_from_token(token)
            if symbol:
                if symbol not in self._enabled_symbols:
                    continue
                with self._lock:
                    self._index_prices[symbol] = ltp
                    if isinstance(exch_ts, datetime):
                        self._last_exchange_ts_by_symbol[symbol] = exch_ts
                self._maybe_refresh_option_tokens(symbol, ltp)
                self._emit(symbol, now)
                continue

            meta = self._option_meta_by_token.get(token)
            if not meta:
                continue
            symbol = str(meta["symbol"])
            if symbol not in self._enabled_symbols:
                continue
            strike = str(meta["strike"])
            opt_type = str(meta["type"])
            expiry_key = str(meta.get("expiry_ymd") or "")
            if not expiry_key:
                continue
            with self._lock:
                self._option_chain.setdefault(symbol, {})
                self._option_chain[symbol].setdefault(expiry_key, {})
                self._option_chain[symbol][expiry_key].setdefault(strike, {})
                self._option_chain[symbol][expiry_key][strike][opt_type] = {
                    "ltp": float(ltp),
                    "volume": float(norm.get("volume") or 0.0),
                    "oi": float(norm.get("oi") or 0.0),
                    "expiry": str(meta.get("expiry_raw") or ""),
                    "expiry_ymd": expiry_key,
                    "token": str(token),
                    "option_timestamp": (
                        float(exch_ts.timestamp()) if isinstance(exch_ts, datetime) else float(now)
                    ),
                }
                if isinstance(exch_ts, datetime):
                    self._last_exchange_ts_by_symbol[symbol] = exch_ts
            self._emit(symbol, now)

    def _emit(self, symbol: str, now_epoch: float) -> None:
        if symbol not in self._enabled_symbols:
            return
        if self._handler is None or self._loop is None:
            return
        with self._lock:
            index_price = self._index_prices.get(symbol)
            chain = self._option_chain.get(symbol, {})
            exch_ts = self._last_exchange_ts_by_symbol.get(symbol)
        fresh_chain = self._fresh_option_chain(chain, now_epoch)
        if fresh_chain:
            chain = fresh_chain
        if index_price is None or not chain:
            last = float(self._emit_skip_log_ts.get(symbol) or 0.0)
            if (now_epoch - last) >= 5.0:
                self._emit_skip_log_ts[symbol] = now_epoch
                logger.info(
                    "Emit skipped symbol=%s index_price=%s chain_strikes=%s",
                    symbol,
                    index_price,
                    sum(len(v) for v in chain.values()),
                )
            return
        if isinstance(exch_ts, datetime):
            real_age_ms = max(0.0, (datetime.fromtimestamp(now_epoch, tz=timezone.utc) - exch_ts).total_seconds() * 1000.0)
            # Many exchange timestamps arrive only with second precision.
            # For coarse timestamps, allow extra slack to avoid false stale drops.
            max_age_ms = float(settings.ws_max_tick_age_ms)
            coarse_ts = exch_ts.microsecond == 0
            if coarse_ts:
                max_age_ms = max(max_age_ms, 1500.0)
            if real_age_ms > max_age_ms:
                logger.warning(
                    "Dropping stale tick symbol=%s real_latency_ms=%.2f max_ms=%.2f coarse_ts=%s",
                    symbol,
                    real_age_ms,
                    max_age_ms,
                    coarse_ts,
                )
                return
        tick = MarketTick(
            symbol=symbol,  # type: ignore[arg-type]
            index_price=float(index_price),
            exchange_timestamp=exch_ts,
            timestamp=exch_ts or datetime.now(timezone.utc),
            received_at=datetime.fromtimestamp(now_epoch, tz=timezone.utc),
            option_chain=chain,
            meta={"source": "angel_ws_v2"},
        )
        try:
            self._bridge_queue.put_nowait(tick)
        except queue.Full:
            try:
                self._bridge_queue.get_nowait()
                self._bridge_queue.put_nowait(tick)
                logger.warning("Tick bridge queue full, dropped_oldest_enqueued_new symbol=%s", symbol)
            except queue.Empty:
                logger.warning("Tick bridge queue full and empty-on-pop race, dropping tick symbol=%s", symbol)
            except queue.Full:
                logger.warning("Tick bridge queue still full, dropping tick symbol=%s", symbol)

    def _maybe_refresh_option_tokens(self, symbol: str, spot: float) -> None:
        step = 50.0 if symbol == "NIFTY" else 100.0
        atm = round(spot / step) * step
        last = self._current_atm.get(symbol)
        if last is not None and abs(last - atm) < step:
            return
        self._current_atm[symbol] = atm
        selection = self._select_option_tokens(symbol, atm)
        if not selection:
            return
        self._desired_option_tokens_by_symbol[symbol] = selection
        merged: Dict[str, Tuple[int, str]] = {}
        for per_symbol in self._desired_option_tokens_by_symbol.values():
            merged.update(per_symbol)
        self._sync_option_subscriptions(merged)

    def _sync_option_subscriptions(self, new_tokens: Dict[str, Tuple[int, str]]) -> None:
        prev_map: Dict[str, Tuple[int, str]]
        with self._lock:
            prev_map = dict(self._subscribed_option_tokens)
            prev = set(prev_map.keys())
            current = set(new_tokens.keys())
            to_add = current - prev
            to_remove = prev - current
            self._subscribed_option_tokens = dict(new_tokens)

        if self._ws is None:
            return
        corr = f"OSI{int(time.time())%1000000:06d}"
        if to_remove and hasattr(self._ws, "unsubscribe"):
            by_ex = self._group_tokens_by_exchange({t: prev_map[t] for t in to_remove if t in prev_map})
            for ex, tokens in by_ex.items():
                self._ws.unsubscribe(corr, 1, [{"exchangeType": ex, "tokens": tokens}])
        if to_add:
            by_ex = self._group_tokens_by_exchange({t: new_tokens[t] for t in to_add})
            for ex, tokens in by_ex.items():
                self._ws.subscribe(corr, 1, [{"exchangeType": ex, "tokens": tokens}])
        logger.info("Subscribed option tokens count=%s", len(new_tokens))

    def _subscribe_index_tokens(self) -> None:
        if self._ws is None:
            return
        token_list = [{"exchangeType": ex, "tokens": [token]} for _, (ex, token) in self._index_tokens.items()]
        logger.info("Index subscribe payload=%s", token_list)
        self._ws.subscribe("OSIINDEX01", 1, token_list)

    def _resubscribe_option_tokens(self) -> None:
        # Ensure option subscriptions are re-applied after reconnect.
        with self._lock:
            merged: Dict[str, Tuple[int, str]] = {}
            for per_symbol in self._desired_option_tokens_by_symbol.values():
                merged.update(per_symbol)
            prices = dict(self._index_prices)
        if not merged:
            for symbol, px in prices.items():
                if px and px > 0:
                    self._maybe_refresh_option_tokens(symbol, px)
            with self._lock:
                for per_symbol in self._desired_option_tokens_by_symbol.values():
                    merged.update(per_symbol)
        if merged:
            self._sync_option_subscriptions(merged)

    def _select_option_tokens(self, symbol: str, atm: float) -> Dict[str, Tuple[int, str]]:
        # Prefer today's expiry first for 0DTE; fallback chooses nearest available in instrument master.
        preferred_expiry = datetime.now(timezone.utc).strftime("%d%b%Y").upper()
        expiry = self._resolve_available_expiry(symbol, preferred_expiry)
        if expiry != preferred_expiry:
            logger.info(
                "Expiry fallback for %s preferred=%s resolved=%s",
                symbol,
                preferred_expiry,
                expiry,
            )
        step = 50.0 if symbol == "NIFTY" else 100.0
        strike_targets = [atm - step, atm, atm + step]
        rows = [r for r in self._instrument_rows if self._match_option_row(r, symbol, expiry)]
        selected: Dict[str, Tuple[int, str]] = {}
        selected_meta: Dict[str, Dict[str, Any]] = {}
        for strike in strike_targets:
            for opt_type in ("CE", "PE"):
                row = self._closest_contract(rows, strike, opt_type)
                if not row:
                    continue
                token = self._normalize_token(row.get("token"))
                if not token:
                    continue
                ex = 2 if symbol == "NIFTY" else 4
                selected[token] = (ex, token)
                exp_raw = str(expiry or "").upper()
                exp_ymd = self._expiry_to_yyyymmdd(exp_raw)
                if not exp_ymd or self._is_expired_expiry(exp_ymd):
                    continue
                selected_meta[token] = {
                    "symbol": symbol,
                    "strike": int(self._extract_strike(row)),
                    "type": opt_type,
                    "expiry_raw": exp_raw,
                    "expiry_ymd": exp_ymd,
                    "token": token,
                    "option_symbol": f"{symbol}_{exp_ymd}_{int(self._extract_strike(row))}_{opt_type}",
                }
        with self._lock:
            self._option_meta_by_token.update(selected_meta)
        return selected

    def _resolve_available_expiry(self, symbol: str, preferred_expiry: str) -> str:
        rows_for_symbol = [r for r in self._instrument_rows if self._match_option_row(r, symbol, preferred_expiry)]
        if rows_for_symbol:
            return preferred_expiry

        candidates: List[tuple[datetime, str]] = []
        for row in self._instrument_rows:
            exch = str(row.get("exch_seg") or "").upper()
            if symbol == "NIFTY" and exch != "NFO":
                continue
            if symbol == "SENSEX" and exch != "BFO":
                continue
            if str(row.get("instrumenttype") or "").upper() != "OPTIDX":
                continue
            name = str(row.get("name") or "").upper()
            if symbol == "NIFTY" and name != "NIFTY":
                continue
            if symbol == "SENSEX" and name not in ("SENSEX", "SENSEX50"):
                continue
            sym = str(row.get("symbol") or "").upper()
            if not sym.endswith(("CE", "PE")):
                continue
            exp = str(row.get("expiry") or "").strip().upper()
            if not exp:
                continue
            try:
                dt = datetime.strptime(exp, "%d%b%Y")
            except ValueError:
                continue
            candidates.append((dt, exp))

        if not candidates:
            return preferred_expiry
        today = date.today()
        unique_sorted = sorted(set(candidates), key=lambda x: x[0])
        future = [x for x in unique_sorted if x[0].date() >= today]
        if future:
            return future[0][1]
        return unique_sorted[-1][1]

    def _build_index_tokens(self) -> Dict[str, Tuple[int, str]]:
        out: Dict[str, Tuple[int, str]] = {}
        for row in self._instrument_rows:
            sym = str(row.get("symbol") or "").upper()
            exch = str(row.get("exch_seg") or "").upper()
            token = self._normalize_token(row.get("token"))
            if sym == "NIFTY" and token:
                out["NIFTY"] = (1 if exch == "NSE" else 1, token)
            elif sym == "SENSEX" and token and bool(settings.enable_sensex):
                out["SENSEX"] = (3 if exch in ("BSE", "BSE_CM") else 3, token)
        out.setdefault("NIFTY", (1, "26000"))
        if bool(settings.enable_sensex):
            out.setdefault("SENSEX", (3, "99919000"))
        return out

    def _index_symbol_from_token(self, token: str) -> Optional[str]:
        for sym, (_ex, t) in self._index_tokens.items():
            if token == t:
                return sym
        return None

    @staticmethod
    def _normalize_token(token: Any) -> str:
        t = str(token or "").strip()
        if t.isdigit():
            return str(int(t))
        return t

    @staticmethod
    def _extract_ltp(item: Dict[str, Any]) -> Optional[float]:
        raw = item.get("last_traded_price")
        if raw is not None:
            try:
                return float(raw) / 100.0
            except (TypeError, ValueError):
                pass
        for key in ("ltp", "lastPrice", "close"):
            val = item.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_rows(message: Any) -> List[Dict[str, Any]]:
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8", errors="ignore")
            except Exception:
                return []
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return []
        if isinstance(message, dict):
            data = message.get("data") or message.get("feeds") or message
            if isinstance(data, dict):
                nested = data.get("data") or data.get("ticks")
                if isinstance(nested, list):
                    return [x for x in nested if isinstance(x, dict)]
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                return [data]
        if isinstance(message, list):
            return [x for x in message if isinstance(x, dict)]
        return []

    def _normalize_tick(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "token": self._normalize_token(
                row.get("token")
                or row.get("symbolToken")
                or row.get("symboltoken")
                or row.get("tk")
            ),
            "ltp": self._extract_ltp(row),
            "volume": self._extract_float(row, ["volume_trade_for_the_day", "volume", "v", "vol"]),
            "oi": self._extract_float(row, ["open_interest", "oi", "openInterest"]),
            "exchange_timestamp": self._extract_exchange_timestamp(row),
        }

    @staticmethod
    def _extract_exchange_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        now_utc = datetime.now(timezone.utc)
        candidates = [
            row.get("exchange_timestamp"),
            row.get("exchangeTime"),
            row.get("exch_feed_time"),
            row.get("last_traded_time"),
            row.get("ltt"),
            row.get("timestamp"),
            row.get("ft"),
        ]
        for raw in candidates:
            if raw is None:
                continue
            # epoch in sec/ms/us/ns
            try:
                if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().lstrip("-").isdigit()):
                    iv = int(float(str(raw).strip()))
                    if iv <= 0:
                        continue
                    if iv > 10**18:
                        iv = iv // 10**9
                    elif iv > 10**15:
                        iv = iv // 10**6
                    elif iv > 10**12:
                        iv = iv // 10**3
                    dt = datetime.fromtimestamp(iv, tz=timezone.utc)
                    if abs((now_utc - dt).total_seconds()) > 86400:
                        continue
                    return dt
            except Exception:
                pass
            if isinstance(raw, str):
                txt = raw.strip()
                if not txt:
                    continue
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
                    try:
                        # Vendor strings are commonly in IST with no timezone attached.
                        dt = datetime.strptime(txt, fmt).replace(tzinfo=ist_tz).astimezone(timezone.utc)
                        if abs((now_utc - dt).total_seconds()) > 86400:
                            continue
                        return dt
                    except ValueError:
                        continue
                try:
                    dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ist_tz)
                    dt_utc = dt.astimezone(timezone.utc)
                    if abs((now_utc - dt_utc).total_seconds()) > 86400:
                        continue
                    return dt_utc
                except Exception:
                    continue
        return None

    @staticmethod
    def _fresh_option_chain(
        chain: Dict[str, Dict[str, Dict[str, Dict[str, float]]]], now_epoch: float
    ) -> Dict[str, Dict[str, Dict[str, Dict[str, float]]]]:
        out: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}
        stale_sec = float(settings.option_data_stale_sec)
        for expiry, by_strike in (chain or {}).items():
            expiry_rows: Dict[str, Dict[str, Dict[str, float]]] = {}
            for strike, row in (by_strike or {}).items():
                ce = (row or {}).get("CE") or {}
                pe = (row or {}).get("PE") or {}
                ce_age = now_epoch - float(ce.get("option_timestamp") or 0.0) if ce else 1e9
                pe_age = now_epoch - float(pe.get("option_timestamp") or 0.0) if pe else 1e9
                new_row: Dict[str, Dict[str, float]] = {}
                if ce and ce_age <= stale_sec:
                    new_row["CE"] = {
                        "ltp": float(ce.get("ltp") or 0.0),
                        "volume": float(ce.get("volume") or 0.0),
                        "oi": float(ce.get("oi") or 0.0),
                        "expiry": str(ce.get("expiry") or ""),
                        "expiry_ymd": str(ce.get("expiry_ymd") or expiry),
                        "token": str(ce.get("token") or ""),
                        "option_timestamp": float(ce.get("option_timestamp") or 0.0),
                    }
                if pe and pe_age <= stale_sec:
                    new_row["PE"] = {
                        "ltp": float(pe.get("ltp") or 0.0),
                        "volume": float(pe.get("volume") or 0.0),
                        "oi": float(pe.get("oi") or 0.0),
                        "expiry": str(pe.get("expiry") or ""),
                        "expiry_ymd": str(pe.get("expiry_ymd") or expiry),
                        "token": str(pe.get("token") or ""),
                        "option_timestamp": float(pe.get("option_timestamp") or 0.0),
                    }
                if new_row:
                    expiry_rows[strike] = new_row
            if expiry_rows:
                out[expiry] = expiry_rows
        return out

    @staticmethod
    def _expiry_to_yyyymmdd(expiry_raw: str) -> str:
        txt = str(expiry_raw or "").strip().upper()
        if not txt:
            return ""
        try:
            return datetime.strptime(txt, "%d%b%Y").strftime("%Y%m%d")
        except ValueError:
            return ""

    @staticmethod
    def _is_expired_expiry(expiry_ymd: str) -> bool:
        try:
            exp = datetime.strptime(str(expiry_ymd), "%Y%m%d").date()
        except ValueError:
            return True
        return exp < date.today()

    @staticmethod
    def _extract_float(row: Dict[str, Any], keys: List[str]) -> float:
        for k in keys:
            if row.get(k) is None:
                continue
            try:
                return float(row.get(k))
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _extract_strike(row: Dict[str, Any]) -> float:
        raw = row.get("strike")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if v > 100000:
            return v / 100.0
        if 5000 <= v <= 100000:
            return v
        return v / 100.0

    @staticmethod
    def _match_option_row(row: Dict[str, Any], symbol: str, expiry: str) -> bool:
        exch = str(row.get("exch_seg") or "").upper()
        if symbol == "NIFTY" and exch != "NFO":
            return False
        if symbol == "SENSEX" and exch != "BFO":
            return False
        if str(row.get("instrumenttype") or "").upper() != "OPTIDX":
            return False
        name = str(row.get("name") or "").upper()
        if symbol == "NIFTY" and name != "NIFTY":
            return False
        if symbol == "SENSEX" and name not in ("SENSEX", "SENSEX50"):
            return False
        exp = str(row.get("expiry") or "").strip().upper()
        if exp != expiry:
            return False
        sym = str(row.get("symbol") or "").upper()
        return sym.endswith("CE") or sym.endswith("PE")

    def _closest_contract(self, rows: List[Dict[str, Any]], strike: float, opt_type: str) -> Optional[Dict[str, Any]]:
        candidates = []
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if not sym.endswith(opt_type):
                continue
            s = self._extract_strike(row)
            candidates.append((abs(s - strike), row))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    @staticmethod
    def _group_tokens_by_exchange(token_map: Dict[str, Tuple[int, str]]) -> Dict[int, List[str]]:
        by_ex: Dict[int, List[str]] = {}
        for _token, (ex, tok) in token_map.items():
            by_ex.setdefault(ex, []).append(tok)
        return by_ex

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(2.0)
            last = float(self._last_any_tick_ts or 0.0)
            if last <= 0:
                continue
            age = time.time() - last
            if age > float(settings.ws_heartbeat_stale_sec):
                logger.warning("Heartbeat stale age=%.2fs forcing websocket reconnect", age)
                try:
                    ws = self._ws
                    if ws is not None and hasattr(ws, "close_connection"):
                        ws.close_connection()
                except Exception:
                    pass

