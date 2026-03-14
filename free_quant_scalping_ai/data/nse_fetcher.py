from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from config.settings import settings
from data.data_storage import upsert_option_chain
from utils.logger import get_logger


logger = get_logger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_NSE_HEADERS)
        # Warm up to obtain cookies; without this NSE can return `{}`.
        try:
            _session.get("https://www.nseindia.com/", timeout=10)
        except Exception:
            pass
    return _session


def _fetch_raw_option_chain(symbol: str) -> dict[str, Any] | None:
    url = settings.nse_option_chain_url.format(symbol=symbol)
    try:
        sess = _get_session()
        resp = sess.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning("NSE option chain HTTP %s for %s", resp.status_code, symbol)
            return None
        data = resp.json()
        if not data or data == {}:
            logger.warning("NSE option chain empty JSON for %s", symbol)
        return data
    except Exception as exc:
        logger.warning("Error fetching NSE option chain %s: %s", symbol, exc)
        return None


def fetch_and_store_option_chain(symbol: str) -> pd.DataFrame:
    data = _fetch_raw_option_chain(symbol)
    if not data:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    ts = datetime.now(timezone.utc)
    underlying_symbol = data.get("records", {}).get("underlying", symbol)

    for row in data.get("records", {}).get("data", []):
        expiry = row.get("expiryDate")
        strike = float(row.get("strikePrice", 0.0))

        ce = row.get("CE")
        if ce:
            records.append(
                {
                    "symbol": underlying_symbol,
                    "ts": ts,
                    "strike": strike,
                    "option_type": "CE",
                    "expiry": expiry,
                    "ltp": ce.get("lastPrice"),
                    "volume": ce.get("totalTradedVolume"),
                    "oi": ce.get("openInterest"),
                    "change_oi": ce.get("changeinOpenInterest"),
                }
            )

        pe = row.get("PE")
        if pe:
            records.append(
                {
                    "symbol": underlying_symbol,
                    "ts": ts,
                    "strike": strike,
                    "option_type": "PE",
                    "expiry": expiry,
                    "ltp": pe.get("lastPrice"),
                    "volume": pe.get("totalTradedVolume"),
                    "oi": pe.get("openInterest"),
                    "change_oi": pe.get("changeinOpenInterest"),
                }
            )

    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df["expiry"] = pd.to_datetime(df["expiry"], dayfirst=True, errors="coerce")
        upsert_option_chain(df)
        logger.info("Stored option chain snapshot for %s rows=%s", symbol, len(df))
    return df

