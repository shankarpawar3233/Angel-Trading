"""Export today's IST signal slices plus full storage/signals.json into analysis/."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage" / "signals.json"
OUT_DIR = ROOT / "analysis"


def parse_ts(s: object) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def main() -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    today_ist = datetime.now(IST).date()

    def is_today_ist(dt: datetime | None) -> bool:
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).date() == today_ist

    data = json.loads(STORAGE.read_text(encoding="utf-8"))

    def exec_record_today(rec: dict) -> bool:
        for key in ("entry_time", "created_at"):
            dt = parse_ts(rec.get(key))
            if dt and is_today_ist(dt):
                return True
        return False

    executed_today: list = []
    for rec in list(data.get("active_signals") or []):
        if exec_record_today(rec):
            executed_today.append(rec)
    for rec in list(data.get("history") or []):
        if exec_record_today(rec):
            executed_today.append(rec)

    paper_today = [p for p in (data.get("paper_signals") or []) if is_today_ist(parse_ts(p.get("timestamp")))]
    rej_today = [r for r in (data.get("rejected_signals") or []) if is_today_ist(parse_ts(r.get("timestamp")))]

    out = {
        "export_meta": {
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "date_ist": today_ist.isoformat(),
            "source": str(STORAGE),
            "counts": {
                "today_executed": len(executed_today),
                "today_paper": len(paper_today),
                "today_rejected": len(rej_today),
                "storage_active_signals": len(data.get("active_signals") or []),
                "storage_history": len(data.get("history") or []),
                "storage_paper_total": len(data.get("paper_signals") or []),
                "storage_rejected_total": len(data.get("rejected_signals") or []),
            },
        },
        "today_executed_signals": executed_today,
        "today_paper_signals": paper_today,
        "today_rejected_signals": rej_today,
        "storage_full": data,
    }

    out_path = OUT_DIR / f"export-signals-all-{today_ist.isoformat()}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    print(main())
