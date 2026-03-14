"""
Angel One SmartAPI client for fetching NIFTY 5-minute historical candles.
Fetches in larger date chunks; falls back to mock data on API failure.
"""

import os
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import numpy as np

# Add project root for imports
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import settings
from config.calendar import get_trading_dates, is_weekday


def _get_angel_api():
    """Return Angel One API session if credentials exist; else None."""
    try:
        from SmartApi import SmartConnect
        api_key = os.environ.get("ANGEL_API_KEY") or os.environ.get("SMARTAPI_KEY")
        if not api_key:
            return None
        obj = SmartConnect(api_key=api_key)
        client_code = os.environ.get("ANGEL_CLIENT_ID", "")
        password = os.environ.get("ANGEL_PASSWORD", "")
        totp = os.environ.get("ANGEL_TOTP", "")
        if not (client_code and password and totp):
            return None
        # TOTP typically needs pyotp; skip auth if not available
        try:
            import pyotp
            totp_obj = pyotp.TOTP(totp)
            data = obj.generateSession(client_code, password, totp_obj.now())
            if data.get("status") and data.get("data", {}).get("jwtToken"):
                return obj
        except Exception:
            pass
        return None
    except ImportError:
        return None
    except Exception:
        return None


def _candle_from_angel(
    api,
    exchange: str,
    symbol_token: int,
    interval: str,
    from_ts: str,
    to_ts: str,
    max_retries: int = 3,
    retry_delay: float = 0.5,
) -> list:
    """
    Fetch candles for a date range from Angel API with basic retry on AB1004.
    """
    payload = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": interval,
        "fromdate": from_ts,
        "todate": to_ts,
    }
    for attempt in range(1, max_retries + 1):
        try:
            hist = api.getCandleData(payload)
        except Exception:
            hist = None
        if not hist:
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return []
        # SmartAPI returns dict with status / errorcode / data
        if isinstance(hist, dict):
            if hist.get("status") and hist.get("data"):
                return hist["data"]
            # Retry on AB1004
            if hist.get("errorcode") == "AB1004" and attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return hist.get("data") or []
        # Sometimes returns list directly
        if isinstance(hist, list):
            return hist
        # Unknown format -> stop
        return []
    return []


def _parse_angel_candle(c) -> dict:
    """Parse one candle from Angel format [timestamp, o, h, l, c, v]."""
    if not c or len(c) < 5:
        return None
    ts = c[0] if isinstance(c[0], str) else datetime.fromtimestamp(int(c[0])).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "timestamp": ts,
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]) if len(c) > 5 else 0.0,
    }


def _generate_mock_ohlcv(dates: list, interval_minutes: int = 5) -> pd.DataFrame:
    """Generate realistic mock OHLCV for NIFTY 5-min, 09:15–15:30, weekdays only."""
    np.random.seed(42)
    base_price = 22450.0
    rows = []
    for d in dates:
        if not is_weekday(datetime.combine(d, datetime.min.time())):
            continue
        start = datetime.combine(d, datetime.min.time().replace(hour=9, minute=15, second=0))
        end = datetime.combine(d, datetime.min.time().replace(hour=15, minute=30, second=0))
        t = start
        price = base_price + np.random.randn() * 50
        while t <= end:
            move = np.random.randn() * 8
            o = price
            price = price + move
            h = max(o, price) + abs(np.random.randn() * 2)
            l = min(o, price) - abs(np.random.randn() * 2)
            c = price
            v = int(np.random.lognormal(8, 1.5))
            rows.append({
                "timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": v,
            })
            t += timedelta(minutes=interval_minutes)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_historical_candles(lookback_days: int = None) -> pd.DataFrame:
    """
    Fetch NIFTY 5-minute candles for lookback_days.
    Uses Angel One SmartAPI in multi-day chunks; on failure uses mock data.
    """
    lookback_days = lookback_days or settings.LOOKBACK_DAYS
    all_candles = []
    api_calls = 0

    api = _get_angel_api()
    if api:
        # Build 90-day window in ~15-day chunks
        today = datetime.now().date()
        start_date: date = today - timedelta(days=lookback_days)
        end_date: date = today
        chunk_days = 15

        print(f"[AngelClient] Using Angel One historical API for last {lookback_days} days...")
        cur_start = start_date
        while cur_start <= end_date:
            chunk_end = cur_start + timedelta(days=chunk_days - 1)
            if chunk_end > end_date:
                chunk_end = end_date
            from_ts = f"{cur_start.strftime('%Y-%m-%d')} 09:15"
            to_ts = f"{chunk_end.strftime('%Y-%m-%d')} 15:30"
            print(f"[AngelClient] Fetching candles: {from_ts} -> {to_ts}")
            api_calls += 1
            candles = _candle_from_angel(
                api,
                settings.EXCHANGE,
                settings.SYMBOL_TOKEN,
                settings.INTERVAL,
                from_ts,
                to_ts,
                max_retries=3,
                retry_delay=0.5,
            )
            # Small delay to avoid throttling
            time.sleep(0.5)
            for c in candles or []:
                row = _parse_angel_candle(c)
                if row:
                    all_candles.append(row)
            cur_start = chunk_end + timedelta(days=1)

        if all_candles:
            df = pd.DataFrame(all_candles)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
            print(f"[AngelClient] Total API calls: {api_calls}")
            print(f"[AngelClient] Total candles fetched: {len(df)}")
            return df

    # Fallback: mock data (use trading dates for weekdays only)
    trading_dates = get_trading_dates(lookback_days)
    print("[AngelClient] Using MOCK OHLCV data (Angel API unavailable or returned no candles).")
    df = _generate_mock_ohlcv(trading_dates, 5)
    print(f"[AngelClient] Total candles (mock): {len(df)}")
    return df


def fetch_latest_candle() -> pd.DataFrame:
    """Fetch the latest 5-min candle (e.g. for live mode). Returns empty or 1-row DataFrame."""
    api = _get_angel_api()
    if not api:
        return pd.DataFrame()
    now = datetime.now()
    from_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    to_ts = now.strftime("%Y-%m-%d %H:%M")
    candles = _candle_from_angel(
        api, settings.EXCHANGE, settings.SYMBOL_TOKEN, settings.INTERVAL, from_ts, to_ts
    )
    if not candles:
        return pd.DataFrame()
    last = _parse_angel_candle(candles[-1])
    if last:
        return pd.DataFrame([last])
    return pd.DataFrame()
