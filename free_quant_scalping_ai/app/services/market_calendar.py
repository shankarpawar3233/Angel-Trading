from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_IST = timezone(timedelta(hours=5, minutes=30))
_CAL_PATH = Path(__file__).resolve().with_name("market_calendar_data.json")


def _load_calendar() -> Dict[str, Any]:
    try:
        raw = _CAL_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _ist_now(now_utc: Optional[datetime] = None) -> datetime:
    now = now_utc or datetime.now(timezone.utc)
    return now.astimezone(_IST)


def _to_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        return None


def is_holiday(now_utc: Optional[datetime] = None) -> bool:
    d = _ist_now(now_utc).date()
    cal = _load_calendar()
    for h in cal.get("holidays", []):
        if not isinstance(h, dict):
            continue
        hd = _to_date(str(h.get("date") or ""))
        if hd == d:
            return True
    return False


def holiday_name(now_utc: Optional[datetime] = None) -> Optional[str]:
    d = _ist_now(now_utc).date()
    cal = _load_calendar()
    for h in cal.get("holidays", []):
        if not isinstance(h, dict):
            continue
        hd = _to_date(str(h.get("date") or ""))
        if hd == d:
            nm = str(h.get("name") or "").strip()
            return nm or "Holiday"
    return None


def is_expiry_day(symbol: str = "NIFTY", now_utc: Optional[datetime] = None) -> bool:
    d = _ist_now(now_utc).date()
    su = str(symbol or "NIFTY").upper()
    cal = _load_calendar()
    rows = ((cal.get("expiries") or {}).get(su) or [])
    for r in rows:
        if not isinstance(r, dict):
            continue
        exd = _to_date(str(r.get("date") or ""))
        if exd == d:
            return True
    return False


def _expiry_type_today(symbol: str = "NIFTY", now_utc: Optional[datetime] = None) -> Optional[str]:
    d = _ist_now(now_utc).date()
    su = str(symbol or "NIFTY").upper()
    cal = _load_calendar()
    rows = ((cal.get("expiries") or {}).get(su) or [])
    for r in rows:
        if not isinstance(r, dict):
            continue
        exd = _to_date(str(r.get("date") or ""))
        if exd == d:
            t = str(r.get("type") or "").strip().lower()
            return t or None
    return None


def _next_expiry(symbol: str = "NIFTY", now_utc: Optional[datetime] = None) -> Optional[str]:
    d = _ist_now(now_utc).date()
    su = str(symbol or "NIFTY").upper()
    cal = _load_calendar()
    rows = ((cal.get("expiries") or {}).get(su) or [])
    dates: list[date] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        exd = _to_date(str(r.get("date") or ""))
        if exd is not None and exd >= d:
            dates.append(exd)
    if not dates:
        return None
    return min(dates).isoformat()


def is_market_open(now_utc: Optional[datetime] = None) -> bool:
    ist = _ist_now(now_utc)
    if ist.weekday() >= 5:
        return False
    if is_holiday(now_utc):
        return False
    t = ist.time()
    return dt_time(9, 15) <= t <= dt_time(15, 30)


def get_market_status(symbol: str = "NIFTY", now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    su = str(symbol or "NIFTY").upper()
    open_now = is_market_open(now_utc)
    holiday = is_holiday(now_utc)
    expiry = is_expiry_day(su, now_utc)
    return {
        "is_open": bool(open_now),
        "is_holiday": bool(holiday),
        "holiday_name": holiday_name(now_utc),
        "is_expiry": bool(expiry),
        "expiry_type": _expiry_type_today(su, now_utc),
        "next_expiry": _next_expiry(su, now_utc),
        "symbol": su,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
