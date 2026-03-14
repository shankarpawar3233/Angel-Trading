from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from config.settings import settings
from data.data_storage import upsert_candles
from utils.logger import get_logger


logger = get_logger(__name__)


# Yahoo Finance limits: 1m = max 8 days, 5m/15m = max 60 days, 1d = no strict limit
_INTRADAY_DAYS = {"1m": 7, "5m": 59, "15m": 59}


def download_history_for_symbol(symbol: str) -> None:
    yahoo_symbol = settings.yahoo_symbols.get(symbol)
    if not yahoo_symbol:
        logger.warning("No Yahoo symbol mapping for %s", symbol)
        return

    end = datetime.now(timezone.utc)

    # Intraday: respect Yahoo's per-interval limits
    for interval in settings.intraday_intervals:
        days = _INTRADAY_DAYS.get(interval, 7)
        start = end - timedelta(days=days)
        logger.info("Downloading %s history %s (last %s days)", symbol, interval, days)
        df = yf.download(
            yahoo_symbol,
            start=start,
            end=end,
            interval=interval,
            progress=False,
        )
        if isinstance(df, pd.DataFrame) and not df.empty:
            upsert_candles(symbol, interval, df)

    # Daily: full history
    start_daily = end - timedelta(days=settings.historical_days)
    logger.info("Downloading %s daily history %s %s", symbol, start_daily, end)
    df_daily = yf.download(
        yahoo_symbol,
        start=start_daily,
        end=end,
        interval=settings.daily_interval,
        progress=False,
    )
    if isinstance(df_daily, pd.DataFrame) and not df_daily.empty:
        upsert_candles(symbol, settings.daily_interval, df_daily)


def bootstrap_historical_data() -> None:
    for index_symbol in settings.indices:
        download_history_for_symbol(index_symbol)

