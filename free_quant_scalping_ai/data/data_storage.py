from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

import pandas as pd

from config.settings import DB_PATH
from utils.logger import get_logger


logger = get_logger(__name__)


@contextmanager
def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts DATETIME NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                UNIQUE(symbol, timeframe, ts)
            );
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS option_chain (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts DATETIME NOT NULL,
                strike REAL NOT NULL,
                option_type TEXT NOT NULL,
                expiry DATE NOT NULL,
                ltp REAL,
                volume REAL,
                oi REAL,
                change_oi REAL,
                UNIQUE(symbol, ts, strike, option_type, expiry)
            );
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts DATETIME NOT NULL,
                category TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS option_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                token TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                ltp REAL,
                volume REAL,
                oi REAL,
                oi_change REAL
            );
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts DATETIME NOT NULL,
                regime TEXT NOT NULL,
                confidence REAL,
                payload_json TEXT
            );
        """
        )

        conn.commit()
        logger.info("SQLite schema initialized at %s", DB_PATH)


def upsert_candles(symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    if df.empty:
        return

    df = df.copy()
    df["symbol"] = symbol
    df["timeframe"] = timeframe
    df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "close",
            "Volume": "volume",
        },
        inplace=True,
    )
    df.reset_index(inplace=True)
    df.rename(columns={"Date": "ts", "Datetime": "ts"}, inplace=True)

    cols = ["symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"]
    df = df[cols].copy()
    # SQLite doesn't accept pandas Timestamp; convert to ISO string
    df["ts"] = pd.to_datetime(df["ts"]).dt.strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO candles
            (symbol, timeframe, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            [tuple(row) for row in df.itertuples(index=False, name=None)],
        )
        conn.commit()


def load_candles(
    symbol: str, timeframe: str, limit: int | None = None
) -> pd.DataFrame:
    query = """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY ts DESC
    """
    params: Iterable = (symbol, timeframe)
    if limit:
        query += " LIMIT ?"
        params = (symbol, timeframe, limit)

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params, parse_dates=["ts"])
    return df.sort_values("ts").reset_index(drop=True)


def upsert_option_chain(df: pd.DataFrame) -> None:
    if df.empty:
        return

    cols = [
        "symbol",
        "ts",
        "strike",
        "option_type",
        "expiry",
        "ltp",
        "volume",
        "oi",
        "change_oi",
    ]
    df = df[cols].copy()

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO option_chain
            (symbol, ts, strike, option_type, expiry, ltp, volume, oi, change_oi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            df.itertuples(index=False, name=None),
        )
        conn.commit()


def latest_option_chain(symbol: str) -> pd.DataFrame:
    with get_connection() as conn:
        ts_df = pd.read_sql_query(
            "SELECT MAX(ts) as ts FROM option_chain WHERE symbol = ?",
            conn,
            params=(symbol,),
            parse_dates=["ts"],
        )
        if ts_df.empty:
            return pd.DataFrame()
        latest_ts = ts_df["ts"].iloc[0]
        # When table is empty MAX(ts) can become NaT depending on parsing
        if latest_ts is None or (hasattr(pd, "isna") and pd.isna(latest_ts)):
            return pd.DataFrame()
        df = pd.read_sql_query(
            """
            SELECT *
            FROM option_chain
            WHERE symbol = ? AND ts = ?
        """,
            conn,
            params=(symbol, latest_ts),
            parse_dates=["ts", "expiry"],
        )
    return df


def store_regime(symbol: str, ts, regime: str, confidence: float | None, payload_json: str | None = None) -> None:
    """Store market regime snapshot for history."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO regime_history (symbol, ts, regime, confidence, payload_json)
            VALUES (?, ?, ?, ?, ?)
        """,
            (symbol, ts, regime, confidence, payload_json or "{}"),
        )
        conn.commit()


def store_signal(
    symbol: str,
    ts,
    category: Literal[
        "scalping",
        "hero_zero",
        "institutional",
        "gamma",
        "expiry",
        "combined",
        "regime",
        "final",
        "final_fast",
    ],
    payload_json: str,
) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO signals (symbol, ts, category, payload_json)
            VALUES (?, ?, ?, ?)
        """,
            (symbol, ts, category, payload_json),
        )
        conn.commit()


def get_signal_history(
    symbol: str,
    category: str = "final",
    limit: int = 100,
) -> list[dict]:
    """
    Load signal history for dashboard. Returns list of {ts, ...payload} with newest first.
    """
    import json
    query = """
        SELECT ts, payload_json
        FROM signals
        WHERE symbol = ? AND category = ?
        ORDER BY ts DESC
        LIMIT ?
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(symbol, category, limit), parse_dates=["ts"])
    out = []
    for _, row in df.iterrows():
        ts = row["ts"]
        try:
            payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
        except Exception:
            payload = {}
        out.append({"ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), **payload})
    return out


def _ist_zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


def _ts_to_ist_date(ts_val) -> date | None:
    if ts_val is None:
        return None
    try:
        t = pd.Timestamp(ts_val)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return t.tz_convert(_ist_zone()).date()
    except Exception:
        try:
            s = str(ts_val).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_ist_zone()).date()
        except Exception:
            return None


def reset_final_fast_signal_history(
    *,
    scope: Literal["today_ist", "all"] = "today_ist",
    jsonl_path: Path | None = None,
) -> dict:
    """
    Remove persisted fast-signal history used by /signal-history and analysis JSONL.

    - ``today_ist``: drop rows whose timestamp falls on the current calendar day in Asia/Kolkata.
    - ``all``: remove every ``final_fast`` row and truncate the JSONL log.

    Returns counts for logging/API responses.
    """
    ist = _ist_zone()
    today_ist = datetime.now(ist).date()
    path = jsonl_path or Path(os.getenv("SIGNAL_RECORD_FILE", "logs/signal_events.jsonl"))
    sqlite_deleted = 0
    jsonl_removed = 0
    jsonl_kept = 0

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, ts FROM signals WHERE category = ?", ("final_fast",))
        rows = cur.fetchall()
        ids_del: list[int] = []
        for rid, ts in rows:
            if scope == "all":
                ids_del.append(int(rid))
                continue
            d = _ts_to_ist_date(ts)
            if d == today_ist:
                ids_del.append(int(rid))
        if ids_del:
            chunk = 400
            for i in range(0, len(ids_del), chunk):
                part = ids_del[i : i + chunk]
                q = "DELETE FROM signals WHERE id IN (%s)" % ",".join("?" * len(part))
                cur.execute(q, part)
            sqlite_deleted = len(ids_del)
        conn.commit()

    if path.is_file():
        if scope == "all":
            try:
                with path.open("r", encoding="utf-8") as f:
                    jsonl_removed = sum(1 for line in f if line.strip())
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                logger.warning("[signals] could not truncate %s: %s", path, exc)
        else:
            try:
                kept: list[str] = []
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                            ts_field = row.get("ts")
                            d = _ts_to_ist_date(ts_field)
                            if d == today_ist:
                                jsonl_removed += 1
                                continue
                        except json.JSONDecodeError:
                            kept.append(line.rstrip("\n"))
                            jsonl_kept += 1
                            continue
                        kept.append(line.rstrip("\n"))
                        jsonl_kept += 1
                path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            except OSError as exc:
                logger.warning("[signals] could not rewrite %s: %s", path, exc)

    logger.info(
        "[signals] reset scope=%s sqlite_deleted=%s jsonl_removed=%s path=%s",
        scope,
        sqlite_deleted,
        jsonl_removed,
        path,
    )
    return {
        "scope": scope,
        "sqlite_deleted": sqlite_deleted,
        "jsonl_removed_lines": jsonl_removed,
        "jsonl_path": str(path),
        "today_ist": str(today_ist),
    }


def insert_option_ticks(symbol: str, ticks: list) -> None:
    """
    Insert option tick rows. Each item: {"token", "ltp", "volume", "oi", "oi_change"}.
    timestamp is set to now (UTC).
    """
    if not ticks:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (symbol, str(t.get("token", "")), ts, t.get("ltp"), t.get("volume"), t.get("oi"), t.get("oi_change"))
        for t in ticks
    ]
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO option_ticks (symbol, token, timestamp, ltp, volume, oi, oi_change)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            rows,
        )
        conn.commit()

