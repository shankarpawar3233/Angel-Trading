from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from osi.core.config import settings

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class ExpiryEngine:
    """Resolve index option expiries from the Angel instrument master (IST trading date)."""

    @staticmethod
    def trading_date(from_dt: datetime | None = None) -> date:
        dt = from_dt or datetime.now(IST)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).date()

    @staticmethod
    def preferred_expiry_raw(from_date: date | None = None) -> str:
        d = from_date or ExpiryEngine.trading_date()
        return d.strftime("%d%b%Y").upper()

    @staticmethod
    def expiry_to_ymd(expiry_raw: str) -> str:
        txt = str(expiry_raw or "").strip().upper()
        if not txt:
            return ""
        try:
            return datetime.strptime(txt, "%d%b%Y").strftime("%Y%m%d")
        except ValueError:
            return ""

    @staticmethod
    def is_expired(expiry_ymd: str, from_date: date | None = None) -> bool:
        today = from_date or ExpiryEngine.trading_date()
        try:
            exp = datetime.strptime(str(expiry_ymd), "%Y%m%d").date()
        except ValueError:
            return True
        return exp < today

    @staticmethod
    def match_option_row(row: Dict[str, Any], symbol: str, expiry: str) -> bool:
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
        if exp != str(expiry or "").strip().upper():
            return False
        sym = str(row.get("symbol") or "").upper()
        return sym.endswith("CE") or sym.endswith("PE")

    def resolve_from_master(
        self,
        symbol: Literal["NIFTY", "SENSEX"],
        instrument_rows: List[Dict[str, Any]],
        preferred_raw: str | None = None,
    ) -> Tuple[str, str]:
        """Return (expiry_raw DDMMMYYYY, expiry_ymd YYYYMMDD) from instrument master."""
        preferred = str(preferred_raw or self.preferred_expiry_raw()).strip().upper()
        if any(self.match_option_row(row, symbol, preferred) for row in instrument_rows):
            return preferred, self.expiry_to_ymd(preferred)

        today = self.trading_date()
        candidates: List[Tuple[date, str]] = []
        for row in instrument_rows:
            if not self._is_symbol_option_row(row, symbol):
                continue
            exp = str(row.get("expiry") or "").strip().upper()
            if not exp:
                continue
            try:
                exp_date = datetime.strptime(exp, "%d%b%Y").date()
            except ValueError:
                continue
            candidates.append((exp_date, exp))

        if not candidates:
            ymd = self.expiry_to_ymd(preferred)
            return preferred, ymd

        unique_sorted = sorted(set(candidates), key=lambda x: x[0])
        future = [item for item in unique_sorted if item[0] >= today]
        chosen_date, chosen_raw = future[0] if future else unique_sorted[-1]
        _ = chosen_date
        return chosen_raw, self.expiry_to_ymd(chosen_raw)

    def select_chain_expiry(
        self,
        symbol: Literal["NIFTY", "SENSEX"],
        chain_keys: List[str],
        instrument_rows: List[Dict[str, Any]],
    ) -> str:
        """Pick the best YYYYMMDD expiry key present in the live option chain."""
        _, target_ymd = self.resolve_from_master(symbol, instrument_rows)
        valid = sorted({str(k) for k in chain_keys if str(k) and not self.is_expired(str(k))})
        if not valid:
            return ""
        if target_ymd and target_ymd in valid:
            return target_ymd
        if target_ymd:
            return min(valid, key=lambda k: abs(int(k) - int(target_ymd)))
        return valid[0]

    @staticmethod
    def _is_symbol_option_row(row: Dict[str, Any], symbol: str) -> bool:
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
        sym = str(row.get("symbol") or "").upper()
        return sym.endswith("CE") or sym.endswith("PE")

    def nearest_expiry(self, symbol: Literal["NIFTY", "SENSEX"], from_date: date | None = None) -> date:
        """Legacy weekday estimator; prefer resolve_from_master for live trading."""
        today = from_date or self.trading_date()
        weekday_map = {"NIFTY": 1, "SENSEX": 4}  # Tuesday / Friday
        target_weekday = weekday_map[symbol]
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
