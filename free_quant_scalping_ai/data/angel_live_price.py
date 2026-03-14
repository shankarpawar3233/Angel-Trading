from __future__ import annotations

"""
Angel One SmartAPI live price integration.

Primary purpose: fetch live index prices for NIFTY and SENSEX.
"""

import os
from typing import Any, Dict, Optional

import pyotp
from SmartApi import SmartConnect  # type: ignore[import]

from data.angel_instruments import get_instrument
from utils.logger import get_logger


logger = get_logger(__name__)

_angel: Optional[SmartConnect] = None
# Session payload stored after successful login for WebSocket credentials
_angel_session_data: Optional[Dict[str, Any]] = None


def login_angel() -> Optional[SmartConnect]:
    """
    Log in to Angel SmartAPI using environment variables.

    ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET must be set.
    """
    global _angel
    api_key = os.getenv("ANGEL_API_KEY")
    client_id = os.getenv("ANGEL_CLIENT_ID")
    password = os.getenv("ANGEL_PASSWORD")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET")

    if not all([api_key, client_id, password, totp_secret]):
        logger.warning("[ANGEL] Missing API credentials in environment; skipping SmartAPI login")
        return None

    try:
        logger.info("[ANGEL] Generating TOTP")
        otp = pyotp.TOTP(totp_secret).now()
        angel = SmartConnect(api_key=api_key)
        data = angel.generateSession(client_id, password, otp)
        if "data" not in data:
            logger.error("[ANGEL] Login failed, response missing data: %s", data)
            return None
        _angel = angel
        global _angel_session_data
        _angel_session_data = dict(data["data"])
        if hasattr(angel, "getfeedToken") and callable(angel.getfeedToken):
            try:
                _angel_session_data["feedToken"] = angel.getfeedToken()
            except Exception:
                pass
        logger.info("[ANGEL] Login successful")
        return _angel
    except Exception as exc:
        logger.error("[ANGEL] Login failed: %s", exc)
        _angel = None
        return None


def get_angel_ws_credentials() -> Optional[Dict[str, str]]:
    """
    Return WebSocket credentials: auth_token, api_key, client_code, feed_token.
    Used by Angel option WebSocket stream. Returns None if login fails.
    """
    global _angel_session_data
    angel = _angel or login_angel()
    if angel is None:
        return None
    payload = _angel_session_data or {}
    jwt_token = payload.get("jwtToken") or payload.get("authToken")
    client_code = payload.get("clientcode") or payload.get("client_code")
    api_key = os.getenv("ANGEL_API_KEY")
    feed_token = payload.get("feedToken")
    if not feed_token and hasattr(angel, "getfeedToken") and callable(angel.getfeedToken):
        try:
            feed_token = angel.getfeedToken()
        except Exception:
            pass
    if not all([jwt_token, client_code, api_key, feed_token]):
        return None
    return {
        "auth_token": jwt_token,
        "api_key": api_key,
        "client_code": client_code,
        "feed_token": feed_token,
    }


def get_angel_price(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch live index price for symbol using Angel SmartAPI LTP.

    Returns:
        {"symbol": "NIFTY", "price": 23682.5, "source": "angel_smartapi"} or None on failure.
    """
    global _angel
    inst = get_instrument(symbol)
    if not inst:
        return None
    exchange = inst["exchange"]
    token = inst["token"]

    if _angel is None:
        _angel = login_angel()
    if _angel is None:
        return None

    def _ltp_once() -> Optional[Dict[str, Any]]:
        ts = symbol.upper()
        quote = _angel.ltpData(
            exchange=exchange,
            tradingsymbol=ts,
            symboltoken=token,
        )
        data = quote.get("data") if isinstance(quote, dict) else None
        if not data or "ltp" not in data:
            logger.warning("[ANGEL] No LTP in response for %s: %s", symbol, quote)
            return None
        price = float(data["ltp"])
        logger.info("[ANGEL] Live price %s = %s", symbol, price)
        return {"symbol": symbol, "price": price, "source": "angel_smartapi"}

    try:
        # First attempt
        out = _ltp_once()
        if out is not None:
            return out
    except Exception as exc:
        logger.warning("[ANGEL] Error fetching price for %s: %s", symbol, exc)

    # If we got here, token may be invalid/expired; try re-login once
    logger.info("[ANGEL] Token expired or invalid, re-authenticating")
    _angel = login_angel()
    if _angel is None:
        return None

    try:
        return _ltp_once()
    except Exception as exc:
        logger.warning("[ANGEL] Error fetching price for %s after re-login: %s", symbol, exc)
        return None

