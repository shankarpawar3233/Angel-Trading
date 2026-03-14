"""
Trading calendar utilities: market hours, weekdays, no-trade window.
"""

from datetime import datetime, time, timedelta
from typing import Tuple

from . import settings


def _to_time(dt) -> time:
    """Convert datetime-like (datetime, pd.Timestamp, np.datetime64) to time."""
    if dt is None:
        return time(9, 15)
    if hasattr(dt, "to_pydatetime"):
        return dt.to_pydatetime().time()
    if hasattr(dt, "time") and callable(getattr(dt, "time")):
        t = dt.time()
        if isinstance(t, time):
            return t
    try:
        import pandas as pd
        return pd.Timestamp(dt).to_pydatetime().time()
    except Exception:
        pass
    if isinstance(dt, datetime):
        return dt.time()
    return time(9, 15)  # fallback


def is_weekday(dt: datetime) -> bool:
    """Return True if date is a weekday (Mon–Fri)."""
    return dt.weekday() < 5


def is_market_hours(dt: datetime) -> bool:
    """Check if datetime is within 09:15–15:30."""
    t = _to_time(dt)
    open_t = time(settings.MARKET_OPEN_HOUR, settings.MARKET_OPEN_MINUTE)
    close_t = time(settings.MARKET_CLOSE_HOUR, settings.MARKET_CLOSE_MINUTE)
    return open_t <= t <= close_t


def is_no_trade_window(dt: datetime) -> bool:
    """Check if in 12:00–13:30 (avoid trading)."""
    t = _to_time(dt)
    start = time(settings.NO_TRADE_START_HOUR, settings.NO_TRADE_START_MINUTE)
    end = time(settings.NO_TRADE_END_HOUR, settings.NO_TRADE_END_MINUTE)
    return start <= t <= end


def is_allowed_prediction_time(dt: datetime) -> bool:
    """Allowed prediction window: 09:20–15:15, excluding 12:00–13:30."""
    t = _to_time(dt)
    pred_start = time(settings.PRED_START_HOUR, settings.PRED_START_MINUTE)
    pred_end = time(settings.PRED_END_HOUR, settings.PRED_END_MINUTE)
    if not (pred_start <= t <= pred_end):
        return False
    return not is_no_trade_window(dt)


def get_trading_dates(lookback_days: int) -> list:
    """Return list of trading dates (weekdays) for the last lookback_days."""
    dates = []
    d = datetime.now().date()
    while len(dates) < lookback_days:
        if is_weekday(datetime.combine(d, time.min)):
            dates.append(d)
        d -= timedelta(days=1)
    return sorted(dates)
