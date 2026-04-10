import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SIGNAL_CUTOFF_STR = "2026-03-19T08:00:00.108263+00:00"
USE_FULL_SESSION_END = True
HORIZON_MIN = 180  # used only if USE_FULL_SESSION_END=False


def parse_dt(s: str) -> datetime:
    # ISO with offset like "2026-03-19T08:00:00.108263+00:00"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_option_ticks(db_path: Path) -> dict[str, list[tuple[datetime, float]]]:
    """
    option_ticks table contains:
      token, timestamp, ltp
    We load all persisted tokens from option_ticks.
    Note: persistence currently inserts both indices under symbol='NIFTY', so we don't filter by symbol here.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT token, timestamp, ltp FROM option_ticks WHERE ltp IS NOT NULL AND ltp != ''",
    )
    rows = cur.fetchall()
    conn.close()

    tok_map: dict[str, list[tuple[datetime, float]]] = {}
    for token, ts_str, ltp in rows:
        if token is None or ts_str is None:
            continue
        try:
            tdt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ltp_f = float(ltp)
        except Exception:
            continue
        tok_map.setdefault(str(token), []).append((tdt, ltp_f))

    for tok, arr in tok_map.items():
        arr.sort(key=lambda x: x[0])
    return tok_map


def _strike_from_row(raw_strike):
    if raw_strike is None or raw_strike == "":
        return None
    try:
        v = float(raw_strike)
    except (TypeError, ValueError):
        return None
    # Mirror data/nifty_option_tokens._strike_from_row
    s = v / 100.0
    if 5000 <= s <= 100000:
        return round(s, 2)
    if 5000 <= v <= 100000:
        return float(v)
    if v > 100000:
        return round(v / 100.0, 2)
    return None


def load_token_meta_for_index(index_name: str, exch_seg: str) -> dict[str, tuple[float, str]]:
    """
    Build mapping token -> (strike, type) for an index from Angel instrument master.
    type is 'CE' or 'PE'.
    """
    # Avoid importing data.angel_instruments here (it imports `requests`).
    # We can use the cached master file instead.
    instruments_path = Path("storage") / "angel_instruments.json"
    if not instruments_path.exists():
        raise FileNotFoundError(f"Missing instrument master cache: {instruments_path}")
    instruments = json.loads(instruments_path.read_text(encoding="utf-8"))
    meta = {}
    index_u = index_name.upper()
    for inst in instruments:
        try:
            if str(inst.get("instrumenttype") or "").upper() != "OPTIDX":
                continue
            exch = str(inst.get("exch_seg") or "").upper()
            if exch != exch_seg.upper():
                continue
            sym = str(inst.get("symbol") or "").upper()
            if not sym.endswith(("CE", "PE")):
                continue
            # index name match: NIFTY in symbol or name
            nm = str(inst.get("name") or "").upper()
            if index_u not in nm and index_u not in sym:
                continue
            opt_type = "CE" if sym.endswith("CE") else "PE"
            strike = _strike_from_row(inst.get("strike"))
            tok = str(inst.get("token") or "").strip()
            if not tok or not tok.isdigit():
                continue
            if strike is None:
                continue
            meta[tok] = (float(strike), opt_type)
        except Exception:
            continue
    return meta


def parse_strike_label(label: str) -> tuple[float | None, str | None]:
    """
    Expected formats:
      - '23000 PE'
      - '23150 CE'
    """
    if not label:
        return None, None
    parts = str(label).strip().split()
    if len(parts) < 2:
        return None, None
    try:
        strike = float(parts[0])
    except Exception:
        return None, None
    opt_type = parts[1].upper()
    if opt_type not in ("CE", "PE"):
        return None, None
    return strike, opt_type


def closest_token_for_entry(
    token_ticks: dict[str, list[tuple[datetime, float]]],
    start: datetime,
    end: datetime,
    entry: float,
) -> tuple[str | None, float | None]:
    """
    Pick a token whose first tick in [start, end] is closest to the signal entry premium.
    This is a heuristic because option_ticks stores tokens only (strike/expiry are not persisted).
    """
    best_tok = None
    best_diff = None

    for tok, arr in token_ticks.items():
        # scan until we reach first tick >= start, but stop at end
        for tdt, ltp in arr:
            if tdt < start:
                continue
            if tdt > end:
                break
            diff = abs(ltp - entry)
            if best_tok is None or diff < best_diff:
                best_tok = tok
                best_diff = diff
            break
    return best_tok, best_diff


def main() -> None:
    base = Path(__file__).resolve().parent
    signal_path = base / "logs" / "signal_events.jsonl"
    db_path = base / "storage" / "market_data.sqlite"

    cutoff = parse_dt(SIGNAL_CUTOFF_STR)
    horizon = timedelta(minutes=HORIZON_MIN)

    if not signal_path.exists():
        raise SystemExit(f"Missing signal file: {signal_path}")
    if not db_path.exists():
        raise SystemExit(f"Missing DB file: {db_path}")

    token_ticks = load_option_ticks(db_path)
    token_meta_nifty = load_token_meta_for_index("NIFTY", "NFO")
    token_meta_sensex = load_token_meta_for_index("SENSEX", "BFO")

    # Determine "full session" end as the last observed tick in option_ticks
    all_tick_ts: list[datetime] = []
    for _tok, arr in token_ticks.items():
        for tdt, _ltp in arr:
            if tdt >= cutoff:
                all_tick_ts.append(tdt)
    end_dt = max(all_tick_ts) if all_tick_ts else (cutoff + horizon)

    def evaluate_for_index(index_u: str, token_meta: dict[str, tuple[float, str]]):
        eligible = []
        with signal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                try:
                    ts = parse_dt(rec.get("ts"))
                except Exception:
                    continue
                if ts <= cutoff:
                    continue
                if rec.get("symbol") != index_u:
                    continue
                sig = rec.get("signal")
                if sig not in ("BUY_CE", "BUY_PE"):
                    continue
                entry = rec.get("entry")
                target = rec.get("target")
                stoploss = rec.get("stoploss")
                strike = rec.get("strike")
                if entry is None or target is None or stoploss is None or strike is None:
                    continue
                try:
                    entry_f = float(entry)
                    target_f = float(target)
                    stoploss_f = float(stoploss)
                except Exception:
                    continue
                eligible.append((ts, sig, strike, entry_f, target_f, stoploss_f, rec.get("decision_reason")))

        stats = {
            "total": 0,
            "token_matched": 0,
            "target_hit": 0,
            "stoploss_hit": 0,
            "fulfilled_target_before_sl": 0,
            "no_hit": 0,
        }

        # event rows (for CSV)
        rows: list[dict] = []

        hit_times_sec: list[float] = []
        fulfilled_targets: list[dict] = []

        for ts, sig, strike_label, entry_f, target_f, stoploss_f, reason in eligible:
            stats["total"] += 1
            strike_val, opt_type = parse_strike_label(strike_label)
            if strike_val is None or opt_type is None:
                continue

            candidate_tokens = [
                tok for tok, (st, typ) in token_meta.items() if typ == opt_type and abs(st - strike_val) <= 0.01
            ]
            tok = None
            best_diff = None
            match_end = ts + timedelta(minutes=3)
            for c_tok in candidate_tokens:
                arr = token_ticks.get(c_tok) or []
                for tdt, ltp in arr:
                    if tdt < ts:
                        continue
                    if tdt > match_end:
                        break
                    diff = abs(ltp - entry_f)
                    if tok is None or diff < best_diff:
                        tok = c_tok
                        best_diff = diff
                    break

            if tok is None:
                continue
            stats["token_matched"] += 1

            arr = token_ticks.get(tok) or []
            t_hit = None
            hit_before_sl = None
            session_end_for_this_signal = end_dt if USE_FULL_SESSION_END else (ts + horizon)

            for tdt, ltp in arr:
                if tdt < ts:
                    continue
                if tdt > session_end_for_this_signal:
                    break
                if ltp >= target_f:
                    hit_before_sl = True
                    t_hit = tdt
                    break
                if ltp <= stoploss_f:
                    hit_before_sl = False
                    t_hit = tdt
                    break

            result = "NO_HIT"
            ltp_hit = None
            time_to_target_sec = None
            if hit_before_sl is True:
                result = "TARGET_HIT"
                stats["target_hit"] += 1
                stats["fulfilled_target_before_sl"] += 1
                if t_hit is not None:
                    time_to_target_sec = (t_hit - ts).total_seconds()
                    hit_times_sec.append(time_to_target_sec)
                    # premium at hit:
                    for tdt, ltp in arr:
                        if tdt == t_hit:
                            ltp_hit = ltp
                            break
                fulfilled_targets.append(
                    {
                        "symbol": index_u,
                        "ts": ts.isoformat(),
                        "signal": sig,
                        "strike_label": strike_label,
                        "strike": strike_val,
                        "type": opt_type,
                        "entry": entry_f,
                        "target": target_f,
                        "stoploss": stoploss_f,
                        "target_points_planned": target_f - entry_f,
                        "ltp_at_target_hit": ltp_hit,
                        "target_hit_at": t_hit.isoformat() if t_hit else None,
                        "time_to_target_sec": time_to_target_sec,
                        "decision_reason": reason,
                        "result": result,
                    }
                )
            elif hit_before_sl is False:
                result = "STOPLOSS_HIT"
                stats["stoploss_hit"] += 1
                for tdt, ltp in arr:
                    if tdt == t_hit:
                        ltp_hit = ltp
                        break
            else:
                stats["no_hit"] += 1

            rows.append(
                {
                    "symbol": index_u,
                    "ts": ts.isoformat(),
                    "signal": sig,
                    "strike_label": strike_label,
                    "entry": entry_f,
                    "target": target_f,
                    "stoploss": stoploss_f,
                    "result": result,
                    "target_hit_at": t_hit.isoformat() if t_hit else None,
                    "time_to_target_sec": time_to_target_sec,
                    "decision_reason": reason,
                }
            )

        # summary by CE/PE
        ce_hits = [r for r in fulfilled_targets if r["type"] == "CE"]
        pe_hits = [r for r in fulfilled_targets if r["type"] == "PE"]
        def avg(xs, key):
            vals = [x.get(key) for x in xs if x.get(key) is not None]
            if not vals:
                return None
            return sum(vals) / len(vals)

        print(f"[TargetCheck:{index_u}] stats:", stats)
        if hit_times_sec:
            avg_time = sum(hit_times_sec) / len(hit_times_sec)
            print(f"[TargetCheck:{index_u}] fulfilled count={len(hit_times_sec)} avg_time_to_target_sec={avg_time:.2f}")
        print(f"[TargetCheck:{index_u}] TARGET hits split: CE={len(ce_hits)} PE={len(pe_hits)}")

        # strike aggregation for fulfilled targets
        agg: dict[tuple[float, str], dict] = {}
        for r in fulfilled_targets:
            k = (float(r["strike"]), r["type"])
            a = agg.setdefault(k, {"count": 0, "avg_time_sec": 0.0})
            a["count"] += 1
            a["avg_time_sec"] = a["avg_time_sec"] + (r["time_to_target_sec"] or 0.0)
        for k, a in agg.items():
            if a["count"] > 0:
                a["avg_time_sec"] = a["avg_time_sec"] / a["count"]
        top_strikes = sorted(agg.items(), key=lambda kv: kv[1]["count"], reverse=True)[:10]

        if top_strikes:
            print(f"[TargetCheck:{index_u}] Top fulfilled strikes (strike,type,count,avg_time_sec):")
            for (strike, typ), a in top_strikes:
                print("  ", strike, typ, a["count"], round(a["avg_time_sec"], 2))

        # Write detailed CSV for hits
        out_dir = base / "storage"
        out_dir.mkdir(parents=True, exist_ok=True)
        hits_csv = out_dir / f"target_fulfillment_hits_{index_u}.csv"
        all_csv = out_dir / f"target_fulfillment_all_{index_u}.csv"

        import csv

        with hits_csv.open("w", encoding="utf-8", newline="") as f:
            fieldnames = [
                "symbol",
                "ts",
                "signal",
                "strike_label",
                "strike",
                "type",
                "entry",
                "target",
                "stoploss",
                "target_points_planned",
                "ltp_at_target_hit",
                "target_hit_at",
                "time_to_target_sec",
                "decision_reason",
                "result",
            ]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in fulfilled_targets:
                w.writerow(r)

        with all_csv.open("w", encoding="utf-8", newline="") as f:
            fieldnames2 = [
                "symbol",
                "ts",
                "signal",
                "strike_label",
                "entry",
                "target",
                "stoploss",
                "result",
                "target_hit_at",
                "time_to_target_sec",
                "decision_reason",
            ]
            w = csv.DictWriter(f, fieldnames=fieldnames2)
            w.writeheader()
            for r in rows:
                w.writerow(r)

        print(f"[TargetCheck:{index_u}] wrote: {hits_csv} (hits) and {all_csv} (all evaluated)")

    print(f"[TargetCheck] cutoff={SIGNAL_CUTOFF_STR} full_session_end={end_dt.isoformat()}")
    print(f"[TargetCheck] tokens loaded in option_ticks: {len(token_ticks)}")

    evaluate_for_index("NIFTY", token_meta_nifty)
    evaluate_for_index("SENSEX", token_meta_sensex)


if __name__ == "__main__":
    main()

