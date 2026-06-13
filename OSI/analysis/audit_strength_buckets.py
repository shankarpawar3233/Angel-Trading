"""Audit today's executed trades by directional strength buckets."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage" / "signals.json"
THRESHOLDS = (0.70, 0.80, 0.85, 0.90)


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def is_today_ist(dt: datetime | None, today_ist) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).date() == today_ist


def load_today_trades(today_ist) -> list[dict[str, Any]]:
    raw = json.loads(STORAGE.read_text(encoding="utf-8-sig"))
    trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in ("active_signals", "history"):
        for rec in raw.get(bucket) or []:
            if not isinstance(rec, dict):
                continue
            signal_id = str(rec.get("signal_id") or "")
            if signal_id and signal_id in seen:
                continue
            for key in ("entry_time", "created_at"):
                dt = parse_ts(rec.get(key))
                if dt and is_today_ist(dt, today_ist):
                    trades.append(rec)
                    if signal_id:
                        seen.add(signal_id)
                    break
    return trades


def bucket_stats(trades: list[dict[str, Any]], min_strength: float) -> dict[str, Any]:
    rows = [t for t in trades if float(t.get("strength") or 0.0) >= min_strength]
    pnls = [float(t.get("pnl")) for t in rows if t.get("pnl") is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    decided = wins + losses
    win_rate = (wins / decided * 100.0) if decided else 0.0
    avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0
    total_pnl = sum(pnls) if pnls else 0.0
    return {
        "min_strength": min_strength,
        "trade_count": len(rows),
        "closed_with_pnl": len(pnls),
        "open_or_unpriced": len(rows) - len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
    }


def main() -> dict[str, Any]:
    today_ist = datetime.now(IST).date()
    trades = load_today_trades(today_ist)
    buckets = [bucket_stats(trades, th) for th in THRESHOLDS]
    return {
        "date_ist": today_ist.isoformat(),
        "source": str(STORAGE),
        "total_trades_today": len(trades),
        "buckets": buckets,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
