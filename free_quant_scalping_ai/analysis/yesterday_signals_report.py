"""Summarize persisted fast signals for a calendar day (UTC date prefix on ts).

Usage (from repo root):
  .\\.venv\\Scripts\\python.exe analysis\\yesterday_signals_report.py 2026-04-10
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "market_data.sqlite"
JSONL = ROOT / "logs" / "signal_events.jsonl"


def analyze_sqlite(day: str) -> dict:
    if not DB.exists():
        return {"error": f"missing db: {DB}"}
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM signals WHERE category='final_fast' AND ts LIKE ?",
        (day + "%",),
    )
    n = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT symbol, ts, payload_json
        FROM signals
        WHERE category='final_fast' AND ts LIKE ?
        ORDER BY ts
        """,
        (day + "%",),
    )
    rows = cur.fetchall()
    conn.close()

    sig_c: Counter[str] = Counter()
    ed_c: Counter[str] = Counter()
    reason_c: Counter[str] = Counter()
    confs: list[float] = []
    directional = 0
    entry_buy = 0
    by_sym: dict[str, dict] = defaultdict(
        lambda: {
            "n": 0,
            "signal": Counter(),
            "entry": Counter(),
            "reason": Counter(),
            "conf": [],
        }
    )

    for sym, _ts, pj in rows:
        try:
            p = json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            p = {}
        s = str(p.get("signal") or p.get("trade") or "NO_TRADE").upper()
        ed = str(p.get("entry_decision") or "HOLD").upper()
        r = str(p.get("decision_reason") or "")
        sig_c[s] += 1
        ed_c[ed] += 1
        reason_c[r] += 1
        try:
            c = float(p.get("confidence") or 0)
            confs.append(c)
            by_sym[sym]["conf"].append(c)
        except Exception:
            pass
        if s in ("BUY_CE", "BUY_PE"):
            directional += 1
        if ed in ("BUY_CE", "BUY_PE"):
            entry_buy += 1
        bs = by_sym[sym]
        bs["n"] += 1
        bs["signal"][s] += 1
        bs["entry"][ed] += 1
        bs["reason"][r] += 1

    out_by_sym = {}
    for sym, d in by_sym.items():
        conf_list = d["conf"]
        out_by_sym[sym] = {
            "rows": d["n"],
            "signal_counts": dict(d["signal"].most_common(8)),
            "entry_decision_counts": dict(d["entry"].most_common(8)),
            "top_reasons": dict(d["reason"].most_common(8)),
            "conf_mean": round(sum(conf_list) / len(conf_list), 2) if conf_list else None,
            "conf_max": max(conf_list) if conf_list else None,
        }

    return {
        "rows": len(rows),
        "count_query": n,
        "signal_counts": dict(sig_c.most_common(12)),
        "entry_decision_counts": dict(ed_c.most_common(12)),
        "top_reasons": dict(reason_c.most_common(15)),
        "directional_rows": directional,
        "entry_buy_rows": entry_buy,
        "conf_mean": round(sum(confs) / len(confs), 2) if confs else None,
        "conf_max": max(confs) if confs else None,
        "first_ts": rows[0][1] if rows else None,
        "last_ts": rows[-1][1] if rows else None,
        "by_symbol": out_by_sym,
    }


def analyze_jsonl(day: str) -> dict | None:
    if not JSONL.exists():
        return None
    per_sym: dict[str, list[dict]] = defaultdict(list)
    total = 0
    with JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = str(o.get("ts") or "")
            if not ts.startswith(day):
                continue
            total += 1
            sym = str(o.get("symbol") or "")
            per_sym[sym].append(o)

    out: dict = {"lines": total, "symbols": {}}
    for sym, lst in per_sym.items():
        sc = Counter(str(x.get("signal") or "NO_TRADE").upper() for x in lst)
        ed = Counter(str(x.get("entry_decision") or "HOLD").upper() for x in lst)
        rc = Counter(str(x.get("decision_reason") or "") for x in lst)
        conf = [float(x.get("confidence") or 0) for x in lst]
        out["symbols"][sym] = {
            "n": len(lst),
            "signal_counts": dict(sc.most_common(8)),
            "entry_decision_counts": dict(ed.most_common(8)),
            "top_reasons": dict(rc.most_common(10)),
            "conf_mean": round(sum(conf) / len(conf), 2) if conf else None,
            "conf_max": max(conf) if conf else None,
            "first_ts": lst[0].get("ts") if lst else None,
            "last_ts": lst[-1].get("ts") if lst else None,
        }
    return out


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-04-10"
    print("=== Signal day report ===")
    print("day (ts prefix match):", day)
    print("db:", DB)
    sql = analyze_sqlite(day)
    if "error" in sql:
        print("sqlite:", sql["error"])
    else:
        print("\n--- SQLite category=final_fast ---")
        print("rows:", sql["rows"], "(count query:", sql["count_query"], ")")
        if sql["rows"]:
            print("first_ts:", sql["first_ts"])
            print("last_ts:", sql["last_ts"])
            print("signal_counts:", json.dumps(sql["signal_counts"], indent=2))
            print("entry_decision_counts:", json.dumps(sql["entry_decision_counts"], indent=2))
            print("directional_rows (signal BUY_*):", sql["directional_rows"])
            print("entry_buy_rows (entry BUY_*):", sql["entry_buy_rows"])
            print("confidence mean/max:", sql["conf_mean"], sql["conf_max"])
            print("top_reasons:", json.dumps(sql["top_reasons"], indent=2))
            print("by_symbol:", json.dumps(sql["by_symbol"], indent=2))

    jl = analyze_jsonl(day)
    print("\n--- logs/signal_events.jsonl (same day prefix) ---")
    if jl is None:
        print("missing:", JSONL)
    elif jl["lines"] == 0:
        print("no lines for day")
    else:
        print("total lines:", jl["lines"])
        print(json.dumps(jl["symbols"], indent=2))


if __name__ == "__main__":
    main()
