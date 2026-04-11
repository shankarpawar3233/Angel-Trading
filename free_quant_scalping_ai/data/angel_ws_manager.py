"""
Central Angel One SmartAPI WebSocket manager.

- Single WS: index LTP (NIFTY/SENSEX) + NFO option chain.
- In-memory caches only; no REST LTP polling on the hot path.
- One login at startup for feed_token (see angel_live_price.login_angel).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_WS_HEALTH_FILE = Path(__file__).resolve().parents[1] / "logs" / "websocket_health.log"
_WS_VERBOSE = os.getenv("ANGEL_WS_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")


def _append_ws_health_line(msg: str) -> None:
    try:
        _WS_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}Z {msg}\n"
        with _WS_HEALTH_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass

# Exchange types (Angel SmartWebSocketV2)
EX_NSE_CM = 1
EX_NFO = 2
EX_BSE_CM = 3

_lock = threading.RLock()
ws_price_cache: Dict[str, float] = {}  # NIFTY, SENSEX -> last LTP
# symbol -> strike -> CE/PE -> leg
option_chain_cache: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {"NIFTY": {}, "SENSEX": {}}

token_to_symbol: Dict[str, str] = {}  # index tokens -> NIFTY / SENSEX
token_to_strike: Dict[str, float] = {}
token_to_type: Dict[str, str] = {}  # CE / PE
token_to_underlying: Dict[str, str] = {}  # option token -> NIFTY/SENSEX
_prev_oi: Dict[str, float] = {}

_option_token_list: List[Dict[str, str]] = []
_option_token_map: Dict[str, Dict[str, Any]] = {}  # token -> {strike, type, symbol, exchangeType}
_credentials: Optional[Dict[str, str]] = None
_chain_update_prints = 0
_sws: Any = None
_running = False
_stream_thread: Optional[threading.Thread] = None
_heartbeat_thread: Optional[threading.Thread] = None

_last_any_tick = 0.0
_last_index_tick = 0.0
_last_index_log = 0.0
_last_chain_log = 0.0
_last_sensex_health_log = 0.0
_reconnect_interval = 3.0
_stale_index_seconds = 45.0

# Debug: unmapped ticks (token not in index map nor option map)
_unmapped_tick_tokens_logged: set = set()
_unmapped_tick_last_summary = 0.0
_MAX_UNMAPPED_PRINT = 40


def _norm_token(tok: Any) -> str:
    """Match tick token to subscription map (Angel may send '026000' vs '26000')."""
    t = str(tok).strip().replace("\x00", "")
    if t.isdigit():
        return str(int(t))
    return t


def _option_strike_type(symbol: str) -> Optional[tuple]:
    """Same strike logic as option discovery (handles NIFTY2531326000CE etc.)."""
    try:
        from data.angel_option_discovery import _parse_option_symbol

        return _parse_option_symbol(symbol)
    except Exception:
        return None


def _extract_ltp(item: Dict[str, Any]) -> Optional[float]:
    ltp = item.get("ltp") or item.get("lastPrice")
    if ltp is None and "last_traded_price" in item:
        try:
            ltp = float(item["last_traded_price"]) / 100.0
        except (TypeError, ValueError):
            pass
    if ltp is None:
        return None
    try:
        return float(ltp)
    except (TypeError, ValueError):
        return None


def _log_subscription_snapshot() -> None:
    """Log every NFO token -> strike/type after maps are built (call holds _lock)."""
    rows = sorted(
        (str(t), str(token_to_underlying.get(t, "?")), float(token_to_strike[t]), token_to_type.get(t, "?"))
        for t in token_to_strike
    )
    n = len(rows)
    print("[WS SUBSCRIBE] NFO mappings:", n, "tokens")
    for tok, und, strike, ot in rows[:50]:
        print("  ", tok, "->", und, strike, ot)
    if n > 50:
        print("  ... +", n - 50, "more (see log)")
    logger.info(
        "[WS SUBSCRIBE] token_to_strike count=%s first_15=%s",
        n,
        rows[:15],
    )
    print("[TOKENS MAPPED]", n + len(token_to_symbol), "(options=", n, "index=", len(token_to_symbol), ")")


def _on_data(_wsapp: Any, message: Any) -> None:
    global _last_any_tick, _last_index_tick, _last_index_log, _last_chain_log, _last_sensex_health_log
    global option_chain_cache, ws_price_cache
    global _unmapped_tick_tokens_logged, _unmapped_tick_last_summary, _chain_update_prints
    try:
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8", errors="ignore")
            except Exception:
                return
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return
        rows: List[Dict[str, Any]] = []
        if isinstance(message, list):
            rows = [x for x in message if isinstance(x, dict)]
        elif isinstance(message, dict):
            data = message.get("data") or message.get("feeds") or message
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                rows = [data]
        now = time.time()
        for item in rows:
            token = _norm_token(
                item.get("symbolToken")
                or item.get("token")
                or item.get("symboltoken")
                or ""
            )
            if not token:
                continue
            ltp = _extract_ltp(item)
            vol = (
                item.get("volume")
                or item.get("volumeTraded")
                or item.get("volume_traded")
                or item.get("volume_trade_for_the_day")
            )
            oi = item.get("oi") or item.get("openInterest") or item.get("open_interest")
            ch_raw = item.get("change_in_open_interest") or item.get("changeInOpenInterest")

            with _lock:
                _last_any_tick = now

                sym = token_to_symbol.get(token)
                if sym and ltp is not None:
                    ws_price_cache[sym.upper()] = ltp
                    _last_index_tick = now
                    if now - _last_index_log >= 2.0:
                        _last_index_log = now
                        try:
                            print("[TICK RECEIVED]", token, sym, ltp)
                        except Exception:
                            pass

                strike = token_to_strike.get(token)
                opt_type = token_to_type.get(token)
                under = token_to_underlying.get(token) or "NIFTY"
                if strike is not None and opt_type in ("CE", "PE"):
                    oi_f = float(oi) if oi is not None else None
                    oi_change = None
                    if ch_raw is not None:
                        try:
                            oi_change = float(ch_raw)
                        except (TypeError, ValueError):
                            pass
                    if oi_change is None and oi_f is not None:
                        prev = _prev_oi.get(token)
                        if prev is not None:
                            oi_change = oi_f - prev
                        _prev_oi[token] = oi_f
                    elif oi_f is not None:
                        _prev_oi[token] = oi_f

                    sk = str(int(strike)) if strike == int(strike) else str(strike)
                    option_chain_cache.setdefault(under, {})
                    option_chain_cache[under].setdefault(sk, {})
                    coi = oi_change if oi_change is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("change_oi")
                    leg = {
                        "ltp": ltp,
                        "oi": float(oi) if oi is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("oi"),
                        "volume": int(vol) if vol is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("volume", 0) or 0,
                        "change_oi": coi,
                        "oi_change": coi,
                        "token": token,
                        "ts": now,
                    }
                    option_chain_cache[under][sk][opt_type] = leg
                    _chain_update_prints += 1
                    if _WS_VERBOSE and (_chain_update_prints <= 30 or _chain_update_prints % 200 == 0):
                        logger.debug("[CHAIN UPDATE] %s %s %s ltp=%s", under, sk, opt_type, leg.get("ltp"))
                elif sym is None:
                    # Option-like tick but token not in token_to_strike (or not CE/PE)
                    if ltp is not None:
                        try:
                            lp = float(ltp)
                        except (TypeError, ValueError):
                            lp = 0.0
                        if lp > 0:
                            sm = item.get("subscription_mode")
                            ex = item.get("exchange_type")
                            if token not in _unmapped_tick_tokens_logged:
                                _unmapped_tick_tokens_logged.add(token)
                                if len(_unmapped_tick_tokens_logged) <= _MAX_UNMAPPED_PRINT:
                                    print(
                                        "[WS UNMAPPED TICK] token=",
                                        token,
                                        "ltp=",
                                        lp,
                                        "mode=",
                                        sm,
                                        "exch=",
                                        ex,
                                        "(not in token_to_symbol / token_to_strike)",
                                    )
                                    logger.warning(
                                        "[WS UNMAPPED TICK] token=%s ltp=%s subscription_mode=%s exchange_type=%s",
                                        token,
                                        lp,
                                        sm,
                                        ex,
                                    )
                            now_u = time.time()
                            if now_u - _unmapped_tick_last_summary >= 120.0:
                                _unmapped_tick_last_summary = now_u
                                logger.info(
                                    "[WS UNMAPPED] unique unmapped tokens so far: %s",
                                    len(_unmapped_tick_tokens_logged),
                                )
        try:
            with _lock:
                n = len(option_chain_cache.get("NIFTY", {})) + len(option_chain_cache.get("SENSEX", {}))
            if n > 0 and now - _last_chain_log >= 15.0:
                _last_chain_log = now
                print("[CHAIN SIZE]", n)
        except Exception:
            pass
        # Feed-health debug: SENSEX CE/PE live leg counts every 10s.
        try:
            if now - _last_sensex_health_log >= 10.0:
                with _lock:
                    sensex_chain = option_chain_cache.get("SENSEX", {})
                    ce_live = 0
                    pe_live = 0
                    for sides in sensex_chain.values():
                        if not isinstance(sides, dict):
                            continue
                        ce = sides.get("CE")
                        pe = sides.get("PE")
                        if isinstance(ce, dict) and ce.get("ltp") is not None:
                            ce_live += 1
                        if isinstance(pe, dict) and pe.get("ltp") is not None:
                            pe_live += 1
                _last_sensex_health_log = now
                print(f"[SENSEX LIVE LEGS] CE={ce_live} PE={pe_live}")
        except Exception:
            pass
    except Exception as exc:
        logger.debug("[WS] tick parse: %s", exc)


def _build_index_maps() -> List[tuple]:
    """Return list of (exchange_type, token, symbol_upper)."""
    out: List[tuple] = []
    try:
        from data.angel_instruments import get_instrument

        n = get_instrument("NIFTY")
        if n and n.get("token"):
            tok = _norm_token(n["token"])
            ex = str(n.get("exchange", "")).upper()
            et = EX_NSE_CM if ex == "NSE" else EX_NSE_CM
            out.append((et, tok, "NIFTY"))
        s = get_instrument("SENSEX")
        if s and s.get("token"):
            tok = _norm_token(s["token"])
            ex = str(s.get("exchange", "")).upper()
            et = EX_BSE_CM if ex in ("BSE", "BSE_CM") else EX_BSE_CM
            out.append((et, tok, "SENSEX"))
    except Exception as exc:
        logger.warning("[WS] index map build: %s", exc)
    if not out:
        out = [(EX_NSE_CM, _norm_token("26000"), "NIFTY"), (EX_BSE_CM, _norm_token("99919000"), "SENSEX")]
    return out


def _run_ws_loop() -> None:
    global _sws, _running, token_to_symbol, token_to_strike, token_to_type, token_to_underlying
    global _unmapped_tick_tokens_logged, _unmapped_tick_last_summary
    try:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    except ImportError:
        logger.error("[WS] SmartWebSocketV2 not installed")
        return

    while _running and _credentials:
        auth_token = _credentials.get("auth_token")
        api_key = _credentials.get("api_key")
        client_code = _credentials.get("client_code")
        feed_token = _credentials.get("feed_token")
        if not all([auth_token, api_key, client_code, feed_token]):
            break

        index_specs = _build_index_maps()
        opt_tokens = [_norm_token(t["token"]) for t in _option_token_list if t.get("token")]
        batch_size = 50
        opt_batches = [opt_tokens[i : i + batch_size] for i in range(0, len(opt_tokens), batch_size)]
        opt_tokens_by_ex: Dict[int, List[str]] = {}
        for t in _option_token_list:
            tok = _norm_token(t.get("token", ""))
            if not tok:
                continue
            ex = int(t.get("exchangeType") or EX_NFO)
            opt_tokens_by_ex.setdefault(ex, []).append(tok)
        opt_batches_by_ex: Dict[int, List[List[str]]] = {
            ex: [vals[i : i + batch_size] for i in range(0, len(vals), batch_size)]
            for ex, vals in opt_tokens_by_ex.items()
        }

        # OI enhancement: choose near-ATM subset PER underlying (NIFTY/SENSEX).
        oi_tokens: List[str] = []
        oi_cap_nifty = int(os.getenv("ANGEL_OI_TOKENS_NIFTY", os.getenv("ANGEL_NFO_OI_TOKENS", "80")))
        oi_cap_sensex = int(os.getenv("ANGEL_OI_TOKENS_SENSEX", "60"))
        if _option_token_map:
            try:
                by_under: Dict[str, List[tuple]] = {}
                for t, m in _option_token_map.items():
                    und = str(m.get("symbol") or "NIFTY").upper()
                    by_under.setdefault(und, []).append((_norm_token(t), float(m.get("strike") or 0.0)))

                for und, rows in by_under.items():
                    if not rows:
                        continue
                    cap = oi_cap_nifty if und == "NIFTY" else oi_cap_sensex if und == "SENSEX" else 0
                    if cap <= 0:
                        continue
                    ref = ws_price_cache.get(und)
                    if ref is None:
                        strikes = sorted(s for _, s in rows)
                        ref = strikes[len(strikes) // 2] if strikes else 0.0
                    ordered = sorted(rows, key=lambda x: abs(x[1] - float(ref or 0.0)))
                    oi_tokens.extend([t for t, _ in ordered[:cap]])
            except Exception:
                oi_tokens = []
        oi_batches = [oi_tokens[i : i + batch_size] for i in range(0, len(oi_tokens), batch_size)] if oi_tokens else []

        with _lock:
            token_to_symbol.clear()
            token_to_strike.clear()
            token_to_type.clear()
            token_to_underlying.clear()
            for et, tok, sym in index_specs:
                token_to_symbol[_norm_token(tok)] = sym
            if _option_token_map:
                for tok, meta in _option_token_map.items():
                    nt = _norm_token(tok)
                    token_to_strike[nt] = float(meta["strike"])
                    token_to_type[nt] = str(meta.get("type") or "CE")
                    token_to_underlying[nt] = str(meta.get("symbol") or "NIFTY").upper()
            else:
                for t in _option_token_list:
                    sym = (t.get("symbol") or "").strip()
                    tok = _norm_token(t.get("token", ""))
                    if not sym or not tok:
                        continue
                    parsed = _option_strike_type(sym)
                    if parsed:
                        strike, ot = parsed
                        token_to_strike[tok] = float(strike)
                        token_to_type[tok] = ot
                        token_to_underlying[tok] = "NIFTY"

            _unmapped_tick_tokens_logged.clear()
            _unmapped_tick_last_summary = 0.0
            _log_subscription_snapshot()

        print("[WS SUBSCRIBE] Option tokens:", len(opt_tokens), opt_tokens[:10])
        if not opt_batches:
            logger.error("[WS] ZERO option tokens — fix get_nifty_option_tokens / NIFTY_PROXY_SPOT")
        total_tokens = sum(len(b) for b in opt_batches) + len(index_specs)
        print("[TOKENS]", total_tokens, "index=", len(index_specs), "options=", len(opt_tokens))
        try:
            _sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)
            def on_open(_wsapp: Any) -> None:
                print("[WS CONNECTED]")
                _append_ws_health_line("CONNECT index+NFO subscription started")
                cid = 0

                def _corrid() -> str:
                    nonlocal cid
                    cid += 1
                    return ("S%09d" % (cid % 1_000_000_000))[:10]

                for et, tok, _sym in index_specs:
                    _sws.subscribe(_corrid(), 1, [{"exchangeType": et, "tokens": [str(tok)]}])
                # NFO LTP for all tokens (stable real-time chain LTP).
                for ex, batches in opt_batches_by_ex.items():
                    for batch in batches:
                        if not batch:
                            continue
                        tl = [{"exchangeType": ex, "tokens": batch}]
                        _sws.subscribe(_corrid(), 1, tl)

                # Optional OI stream on near-ATM subset via SNAP_QUOTE.
                enable_oi = os.getenv("ANGEL_NFO_ENABLE_OI", "1").strip().lower() in ("1", "true", "yes", "on")
                if enable_oi and oi_batches:
                    for batch in oi_batches:
                        if not batch:
                            continue
                        # Split OI subset by exchange (NFO/BFO)
                        ex_to_toks: Dict[int, List[str]] = {}
                        for tk in batch:
                            meta = _option_token_map.get(tk) or _option_token_map.get(_norm_token(tk)) or {}
                            ex = int(meta.get("exchangeType") or EX_NFO)
                            ex_to_toks.setdefault(ex, []).append(tk)
                        for ex, ex_batch in ex_to_toks.items():
                            tl = [{"exchangeType": ex, "tokens": ex_batch}]
                            try:
                                _sws.subscribe(_corrid(), 3, tl)
                            except Exception as exc:
                                logger.warning("[WS] OI SNAP_QUOTE failed on batch: %s", exc)
                sensex_oi = len([t for t in oi_tokens if str(_option_token_map.get(t, {}).get("symbol", "")).upper() == "SENSEX"])
                nifty_oi = len([t for t in oi_tokens if str(_option_token_map.get(t, {}).get("symbol", "")).upper() == "NIFTY"])
                logger.info(
                    "[WS] subscribed index + %s LTP option batches + %s OI batches (NIFTY_OI=%s SENSEX_OI=%s)",
                    len(opt_batches),
                    len(oi_batches),
                    nifty_oi,
                    sensex_oi,
                )

                def _failsafe_options() -> None:
                    time.sleep(5.0)
                    with _lock:
                        nchain = len(option_chain_cache)
                        nmap = len(_option_token_map) if _option_token_map else len(token_to_strike)
                    if nmap > 0 and nchain == 0:
                        print(
                            "[ERROR] No option ticks received — check:\n"
                            "  - tokens not empty\n"
                            "  - exchangeType=2 (NFO)\n"
                            "  - token mapping vs master\n"
                            "  - market open / LTP mode for NFO"
                        )
                        logger.error("[WS] option_chain_cache empty after 5s despite %s mapped tokens", nmap)

                threading.Thread(target=_failsafe_options, daemon=True).start()

            def on_error(_wsapp: Any, error: Any) -> None:
                print("[WS ERROR]", error)
                logger.error("[WS] error: %s", error)
                _append_ws_health_line(f"ERROR {error!s}")
                try:
                    if _sws is not None and hasattr(_sws, "close_connection"):
                        _sws.close_connection()
                except Exception:
                    pass

            def on_close(_wsapp: Any) -> None:
                print("[WS CLOSED] reconnecting in %.0fs" % _reconnect_interval)
                logger.info("[WS] closed; reconnect in %s", _reconnect_interval)
                _append_ws_health_line(f"CLOSE reconnect_in={_reconnect_interval}s")

            _sws.on_data = _on_data
            _sws.on_open = on_open
            _sws.on_error = on_error
            _sws.on_close = on_close
            _sws.connect()
        except Exception as exc:
            logger.exception("[WS] connect failed: %s", exc)
        if not _running:
            break
        time.sleep(_reconnect_interval)


def _heartbeat_loop() -> None:
    global _sws, _running
    while _running:
        time.sleep(2.0)
        try:
            now = time.time()
            with _lock:
                stale = (now - _last_index_tick) > _stale_index_seconds
            if stale and _running and _last_index_tick > 0:
                age = now - _last_index_tick
                print("[WS DEAD] index stale %.0fs — reconnect" % age)
                _append_ws_health_line(f"STALE_INDEX age_sec={age:.0f} forcing_reconnect")
                try:
                    if _sws is not None and hasattr(_sws, "close_connection"):
                        _sws.close_connection()
                except Exception:
                    pass
        except Exception:
            pass


def start_angel_ws_manager(
    option_tokens: Optional[List[Dict[str, str]]] = None,
    credentials: Optional[Dict[str, str]] = None,
    *,
    nifty_token_map: Optional[Dict[str, Dict[str, Any]]] = None,
    option_token_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> bool:
    """
    Start single WebSocket. Pass either nifty_token_map (from get_nifty_option_tokens) or legacy option_tokens.
    """
    global _running, _stream_thread, _heartbeat_thread, _option_token_list, _credentials
    global _option_token_map, _chain_update_prints, option_chain_cache

    _chain_update_prints = 0
    if option_token_map:
        _option_token_map = dict(option_token_map)
        _option_token_list = [
            {
                "token": str(t),
                "symbol": str(m.get("symbol") or "NIFTY"),
                "exchangeType": int(m.get("exchangeType") or EX_NFO),
            }
            for t, m in _option_token_map.items()
        ]
    elif nifty_token_map:
        _option_token_map = dict(nifty_token_map)
        _option_token_list = [{"token": str(t), "symbol": "NIFTY", "exchangeType": EX_NFO} for t in _option_token_map.keys()]
    else:
        _option_token_map = {}
        if option_tokens is None:
            option_tokens = []
        _option_token_list = list(option_tokens)
    if not _option_token_list:
        logger.warning("[WS] index-only (0 NFO tokens); set NIFTY_PROXY_SPOT / NIFTY_ATM_RANGE")

    if credentials is None:
        from data.angel_live_price import get_angel_ws_credentials

        credentials = get_angel_ws_credentials()
    if not credentials:
        logger.warning("[WS] no credentials (login once with ANGEL_* env)")
        return False

    if _running:
        logger.info("[WS] manager already running")
        return True

    # Keep the token list prepared above (from nifty_token_map or option_tokens).
    with _lock:
        option_chain_cache = {"NIFTY": {}, "SENSEX": {}}
    _credentials = dict(credentials)
    _running = True
    _stream_thread = threading.Thread(target=_run_ws_loop, daemon=True)
    _stream_thread.start()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    _heartbeat_thread.start()
    return True


def stop_angel_ws_manager() -> None:
    global _sws, _running
    _running = False
    if _sws is not None and hasattr(_sws, "close_connection"):
        try:
            _sws.close_connection()
        except Exception:
            pass
        _sws = None


def get_feed_health() -> Dict[str, Any]:
    """Age of last index tick and last any tick (for staleness gates in the API)."""
    now = time.time()
    with _lock:
        idx_age = (now - _last_index_tick) if _last_index_tick else None
        any_age = (now - _last_any_tick) if _last_any_tick else None
        return {
            "last_index_tick_age_sec": round(idx_age, 2) if idx_age is not None else None,
            "last_any_tick_age_sec": round(any_age, 2) if any_age is not None else None,
            "index_stale": bool(idx_age is not None and idx_age > _stale_index_seconds),
        }


def get_ws_price(symbol: str) -> Optional[float]:
    with _lock:
        return ws_price_cache.get(symbol.upper())


def get_option_chain_copy(symbol: Optional[str] = "NIFTY") -> Dict[str, Dict[str, Dict[str, Any]]]:
    with _lock:
        if symbol is None:
            # Full multi-symbol copy
            out: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for sym, chain in option_chain_cache.items():
                out[sym] = {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in chain.items()}
            return out  # type: ignore[return-value]
        su = str(symbol).upper()
        chain = option_chain_cache.get(su) or {}
        return {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in chain.items()}
