from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from osi.core.config import settings


class ExpiryEngine:
    weekday_map = {
        "NIFTY": 3,  # Thursday
        "SENSEX": 4,  # Friday
    }

    def nearest_expiry(self, symbol: Literal["NIFTY", "SENSEX"], from_date: date | None = None) -> date:
        today = from_date or date.today()
        target_weekday = self.weekday_map[symbol]
        days_ahead = (target_weekday - today.weekday()) % 7
        expiry = today + timedelta(days=days_ahead)
        holidays = self._holiday_set()
        while expiry.weekday() >= 5 or expiry in holidays:
            expiry -= timedelta(days=1)
        return expiry

    @staticmethod
    def _holiday_set() -> set[date]:
        if not settings.holidays_csv.strip():
            return set()
        out: set[date] = set()
        for raw in settings.holidays_csv.split(","):
            s = raw.strip()
            if not s:
                continue
            try:
                yyyy, mm, dd = s.split("-")
                out.add(date(int(yyyy), int(mm), int(dd)))
            except ValueError:
                continue
        return out

