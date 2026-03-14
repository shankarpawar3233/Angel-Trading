from __future__ import annotations

"""
Angel One SmartAPI WebSocket stream for real-time option chain data.

Uses SmartWebSocketV2 to subscribe to NIFTY option tokens from option discovery.
Maintains in-memory live_option_chain and optionally persists to option_ticks.
"""

import threading
import time
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# NFO exchange type for Angel WebSocket (2 = NSE_FO)
NFO_EXCHANGE_TYPE = 2

# In-memory live option chain: key = "STRIKE{CE|PE}" e.g. "23150CE"
# Value = {"ltp", "volume", "oi", "oi_change", "symbol", "token"}
live_option_chain: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Token -> strike key mapping (e.g. "12345" -> "23150CE") for tick updates
_token_to_key: Dict[str, str] = {}
_stream_thread: Optional[threading.Thread] = None
_sws: Any = None
_running = False


def _symbol_to_chain_key(symbol: str) -> str:
    """Convert NIFTY24MAR23150CE -> 23150CE."""
    s = (symbol or "").strip().upper()
    if "NIFTY" in s:
        # Take the part after NIFTY that ends with CE or PE (strike + type)
        for suffix in ("CE", "PE"):
            if s.endswith(suffix):
                num_part = s[len("NIFTY"):-2]
                # Last digits before CE/PE are strike
                digits = ""
                for c in reversed(num_part):
                    if c.isdigit():
                        digits = c + digits
                    else:
                        break
                if digits:
                    return digits + suffix
    return s


def _on_tick(wsapp: Any, message: Any) -> None:
    """Process incoming tick and update live_option_chain."""
    global live_option_chain, _token_to_key
    try:
        if not isinstance(message, dict):
            return
        # Angel v2 tick format: can be list of tokens or dict with token/key
        data = message.get("data") or message
        if isinstance(data, list):
            for item in data:
                _apply_tick(item)
        elif isinstance(data, dict):
            _apply_tick(data)
    except Exception as exc:
        logger.debug("[WS] Tick parse error: %s", exc)


def _apply_tick(item: Dict[str, Any]) -> None:
    """Apply a single tick to live_option_chain."""
    token = item.get("symbolToken") or item.get("token") or item.get("symboltoken") or ""
    token_str = str(token)
    key = _token_to_key.get(token_str)
    if not key:
        return
    ltp = item.get("ltp") or item.get("lastPrice")
    vol = item.get("volume") or item.get("volumeTraded")
    oi = item.get("oi") or item.get("openInterest")
    if ltp is None and vol is None and oi is None:
        return
    with _lock:
        cur = live_option_chain.get(key, {})
        prev_oi = cur.get("oi")
        oi_val = float(oi) if oi is not None else cur.get("oi")
        oi_change = None
        if oi_val is not None and prev_oi is not None:
            oi_change = oi_val - prev_oi
        elif oi is not None:
            oi_change = float(oi)
        live_option_chain[key] = {
            "ltp": float(ltp) if ltp is not None else cur.get("ltp"),
            "volume": int(vol) if vol is not None else cur.get("volume", 0),
            "oi": float(oi) if oi is not None else cur.get("oi"),
            "oi_change": oi_change if oi_change is not None else cur.get("oi_change"),
            "symbol": cur.get("symbol"),
            "token": cur.get("token") or token_str,
        }


def start_option_stream(
    token_list: List[Dict[str, str]],
    credentials: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Start WebSocket stream in a background thread.
    token_list: [{"symbol": "NIFTY24MAR23150CE", "token": "12345"}, ...]
    credentials: from data.angel_live_price.get_angel_ws_credentials()
    persist_callback: optional callable() to persist live_option_chain to DB periodically.
    """
    global _stream_thread, _running, _token_to_key
    if _running:
        logger.info("[WS] Option stream already running")
        return True

    if not token_list:
        logger.warning("[WS] No tokens to subscribe")
        return False

    try:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    except ImportError as e:
        logger.warning("[WS] SmartWebSocketV2 not available: %s", e)
        return False

    if not credentials:
        from data.angel_live_price import get_angel_ws_credentials
        credentials = get_angel_ws_credentials()
    if not credentials:
        logger.warning("[WS] No Angel credentials; cannot start option stream")
        return False

    auth_token = credentials.get("auth_token")
    api_key = credentials.get("api_key")
    client_code = credentials.get("client_code")
    feed_token = credentials.get("feed_token")
    if not all([auth_token, api_key, client_code, feed_token]):
        logger.warning("[WS] Incomplete credentials")
        return False

    # Build token -> chain key map
    with _lock:
        _token_to_key.clear()
        for t in token_list:
            sym = t.get("symbol", "")
            tok = t.get("token", "")
            if sym and tok:
                key = _symbol_to_chain_key(sym)
                _token_to_key[str(tok)] = key
                live_option_chain.setdefault(key, {"symbol": sym, "token": tok, "ltp": None, "volume": 0, "oi": None, "oi_change": None})

    # NFO exchange type = 2; mode 1 = LTP, 2 = full
    exchange_tokens = [str(t["token"]) for t in token_list]
    # Angel allows max tokens per request; batch if needed
    batch_size = 500
    token_batches = [exchange_tokens[i : i + batch_size] for i in range(0, len(exchange_tokens), batch_size)]

    def _run() -> None:
        global _sws, _running
        try:
            _sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)

            def on_data(wsapp: Any, message: Any) -> None:
                _on_tick(wsapp, message)

            def on_open(wsapp: Any) -> None:
                logger.info("[WS] Angel option stream connected")
                for i, batch in enumerate(token_batches):
                    lst = [{"exchangeType": NFO_EXCHANGE_TYPE, "tokens": batch}]
                    _sws.subscribe(f"nifty_opt_{i}", 1, lst)

            def on_error(wsapp: Any, error: Any) -> None:
                logger.error("[WS] Option stream error: %s", error)

            def on_close(wsapp: Any) -> None:
                logger.info("[WS] Angel option stream closed")

            _sws.on_data = on_data
            _sws.on_open = on_open
            _sws.on_error = on_error
            _sws.on_close = on_close
            _running = True
            _sws.connect()
        except Exception as exc:
            logger.exception("[WS] Option stream failed: %s", exc)
        finally:
            _running = False

    _stream_thread = threading.Thread(target=_run, daemon=True)
    _stream_thread.start()
    return True


def stop_option_stream() -> None:
    """Stop the WebSocket stream."""
    global _sws, _running
    _running = False
    if _sws is not None and hasattr(_sws, "close_connection"):
        try:
            _sws.close_connection()
        except Exception:
            pass
        _sws = None


def get_live_option_chain_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a copy of the current live option chain."""
    with _lock:
        return dict(live_option_chain)


def get_live_option_chain_as_dataframe(symbol_prefix: str = "NIFTY") -> "pd.DataFrame":
    """Return live option chain as DataFrame (strike, option_type, ltp, volume, oi, oi_change) for downstream use."""
    import pandas as pd
    with _lock:
        rows = []
        for key, v in live_option_chain.items():
            if not key.endswith("CE") and not key.endswith("PE"):
                continue
            strike = key[:-2]
            try:
                strike_f = float(strike)
            except ValueError:
                continue
            option_type = "CE" if key.endswith("CE") else "PE"
            rows.append({
                "symbol": symbol_prefix,
                "strike": strike_f,
                "option_type": option_type,
                "ltp": v.get("ltp"),
                "volume": v.get("volume") or 0,
                "oi": v.get("oi"),
                "change_oi": v.get("oi_change"),
            })
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
