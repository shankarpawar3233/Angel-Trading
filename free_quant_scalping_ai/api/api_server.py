from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo

    _IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover — tzdata missing / very old Python
    _IST_TZ = timezone(timedelta(hours=5, minutes=30))

import pandas as pd

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from data.data_storage import (
    get_signal_history,
    init_schema,
    insert_option_ticks,
    load_candles,
    store_signal,
)
from data.historical_fetcher import bootstrap_historical_data
from fast_features import compute_fast_features
from hero_zero_fast import detect_hero_zero_fast
from scalping_fast import compute_fast_confidence, generate_fast_scalping_signal, generate_ml_signal
from training.daily_trainer import train_models
from utils.logger import get_logger

import paper_trader

logger = get_logger(__name__)

app = FastAPI(title="free_quant_scalping_ai")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MarketSnapshot(BaseModel):
    symbol: str
    last_price: float | None
    ts: datetime | None


state: Dict[str, Dict] = {
    "market": {},
    "signals": {},
    "final_signal": {},
    "fast_signals": {},
    "market_regime": {},
    "liquidity_map": {},
    "stop_hunts": {},
    "model_version": None,
}

_scheduler: BackgroundScheduler | None = None
_SIGNAL_LOG_PATH = Path(os.getenv("SIGNAL_RECORD_FILE", "logs/signal_events.jsonl"))
_REPLAY_SIGNAL_LOG_PATH = Path("logs/replay_signals.jsonl")
_SIMULATION_MODE = os.getenv("SIMULATION_MODE", "").strip().lower() in ("1", "true", "yes", "on")
_SIMULATION_SPEED = os.getenv("SIMULATION_SPEED", "").strip().lower()

_REPLAY_LOG_DIR = Path("logs")
_REPLAY_LOCK = threading.Lock()
_REPLAY_STATE: Dict[str, Optional[str]] = {"active_date": None}


def _replay_indian_market_open(now_ist: datetime) -> bool:
    """True iff now_ist is within replay session 09:15–15:30 IST (inclusive)."""
    t = now_ist.time()
    return (t.hour, t.minute, t.second) >= (9, 15, 0) and (t.hour, t.minute, t.second) <= (15, 30, 59)


def _simulation_replay_window_open() -> bool:
    """Simulation replay is allowed only during today's 09:15–15:30 IST session."""
    now_ist = datetime.now(_IST_TZ)
    return _replay_indian_market_open(now_ist)


def _replay_path_for_date(date_str: str) -> Path:
    return _REPLAY_LOG_DIR / f"replay_{date_str}.jsonl"


def _gzip_replay_jsonl(jsonl_path: Path) -> None:
    """Sync gzip of one JSONL to .jsonl.gz; removes source on success."""
    gz_path = jsonl_path.with_name(jsonl_path.name + ".gz")
    if not jsonl_path.is_file():
        return
    if gz_path.is_file():
        return
    with jsonl_path.open("rb") as f_in:
        with gzip.open(gz_path, "wb", compresslevel=6, mtime=0) as f_out:
            shutil.copyfileobj(f_in, f_out)
    try:
        jsonl_path.unlink()
    except OSError:
        pass


def _spawn_replay_gzip(jsonl_path: Path) -> None:
    def _run() -> None:
        try:
            _gzip_replay_jsonl(jsonl_path)
        except Exception:
            pass

    threading.Thread(target=_run, name="replay-gzip", daemon=True).start()


def record_replay_tick(
    symbol: str,
    price: Any,
    chain_snapshot: Any,
    signal: Any,
    entry_decision: Any,
) -> None:
    """Append one JSON line for replay; IST session only; daily rotate + bg gzip."""
    try:
        if price is None:
            return
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p <= 0:
            return
        if not chain_snapshot:
            return
        now_ist = datetime.now(_IST_TZ)
        if not _replay_indian_market_open(now_ist):
            return
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "price": p,
            "option_chain": chain_snapshot,
            "signal": signal,
            "entry_decision": entry_decision,
        }
        line = json.dumps(row, default=str, ensure_ascii=True) + "\n"
        date_str = now_ist.date().isoformat()
        with _REPLAY_LOCK:
            prev = _REPLAY_STATE["active_date"]
            if prev != date_str:
                _REPLAY_STATE["active_date"] = date_str
                if prev:
                    _spawn_replay_gzip(_replay_path_for_date(prev))
            path = _replay_path_for_date(date_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _store_fast_signal_safe(symbol: str, ts: datetime, payload: Dict[str, Any]) -> None:
    """Synchronous helper for writing fast signals to DB (can be run in executor)."""
    try:
        payload_json = json.dumps(payload, default=str)
        store_signal(
            symbol=symbol,
            ts=ts,
            category="final_fast",
            payload_json=payload_json,
        )
    except Exception as exc:
        logger.debug("Failed to store fast signal for %s: %s", symbol, exc)


def _append_signal_record_safe(symbol: str, ts: datetime, payload: Dict[str, Any]) -> None:
    """Append compact signal events to a local JSONL file for debugging/audit."""
    try:
        _SIGNAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": ts.isoformat(),
            "symbol": symbol,
            "signal": payload.get("signal"),
            "entry_decision": payload.get("entry_decision"),
            "decision_reason": payload.get("decision_reason"),
            "confidence": payload.get("confidence"),
            "price": payload.get("price"),
            "strike": payload.get("strike"),
            "entry": payload.get("entry"),
            "target": payload.get("target"),
            "stoploss": payload.get("stoploss"),
        }
        with _SIGNAL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
    except Exception as exc:
        logger.debug("Failed to append signal record for %s: %s", symbol, exc)


def _append_replay_signal_record_safe(symbol: str, ts: datetime, payload: Dict[str, Any], price: float) -> None:
    """Replay-only signal file; generated from live compute path (no file reads)."""
    try:
        _REPLAY_SIGNAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": ts.isoformat(),
            "symbol": symbol,
            "signal": payload.get("signal"),
            "entry_decision": payload.get("entry_decision"),
            "price": price,
            "strike": payload.get("strike"),
            "entry": payload.get("entry"),
            "target": payload.get("target"),
            "stoploss": payload.get("stoploss"),
        }
        with _REPLAY_SIGNAL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
    except Exception as exc:
        logger.debug("Failed to append replay signal record for %s: %s", symbol, exc)


async def compute_for_symbol(symbol: str):
    """
    High-performance live path:
      1) read latest Angel live price
      2) read fast in-memory option chain snapshot (no pandas)
      3) compute fast features
      4) generate scalping + hero-zero signals
      5) compute lightweight confidence
      6) update fast state and (optionally) persist
    """
    from data.angel_ws_manager import get_option_chain_copy, get_ws_price

    su = symbol.upper()

    def _publish_waiting_state(reason: str) -> None:
        ts = datetime.now(timezone.utc)
        waiting = {
            "symbol": symbol,
            "price": 0.0,
            "signal": "NO_TRADE",
            "trade": "NO_TRADE",
            "confidence": 0,
            "entry_decision": "HOLD",
            "decision_reason": reason,
            "stable_count": 0,
            "lock_remaining_sec": 0,
            "hero_zero": None,
            "strike": None,
            "entry": None,
            "target": None,
            "stoploss": None,
            "price_source": "waiting_live",
        }
        state["market"][symbol] = {
            "symbol": symbol,
            "last_price": None,
            "ts": ts.isoformat(),
            "price_source": "waiting_live",
        }
        state["fast_signals"][symbol] = waiting
        state["signals"][symbol] = {
            "trade": "NO_TRADE",
            "confidence": 0,
            "entry_decision": "HOLD",
            "decision_reason": reason,
            "stable_count": 0,
            "lock_remaining_sec": 0,
            "hero_zero": [],
            "oi_analysis": {"pcr": 0.0, "ce_oi_total": 0.0, "pe_oi_total": 0.0, "max_oi_call": None, "max_oi_put": None, "oi_spikes": []},
            "ml": {"label": "NO_TRADE", "confidence": 0},
            "scalping": {
                "trade": "NO_TRADE",
                "confidence": 0,
                "entry_decision": "HOLD",
                "decision_reason": reason,
                "stable_count": 0,
                "strike": None,
                "entry": None,
                "target": None,
                "stoploss": None,
            },
            "symbol": symbol,
            "price": 0.0,
        }
        state["final_signal"][symbol] = {
            "symbol": symbol,
            "price": 0.0,
            "trade": "NO_TRADE",
            "confidence": 0,
            "entry_decision": "HOLD",
            "decision_reason": reason,
            "stable_count": 0,
            "lock_remaining_sec": 0,
            "hero_zero": None,
            "entry": None,
            "target": None,
            "stoploss": None,
            "strike": None,
            "regime": None,
            "gamma_wall": None,
            "max_pain": None,
            "institutional_flow": None,
        }

    price = get_ws_price(su)
    chain_snapshot = get_option_chain_copy(su)
    if not price or float(price) <= 0:
        _publish_waiting_state("waiting_index_price")
        return
    price = float(price)
    price_source = "angel_ws"
    if su == "NIFTY" and not chain_snapshot:
        from data.nse_option_chain import fetch_nifty_option_chain_nse, nse_fallback_enabled

        if nse_fallback_enabled():
            nse_full = fetch_nifty_option_chain_nse()
            chain_snapshot = {}
            for k, v in (nse_full or {}).items():
                try:
                    if abs(float(k) - price) <= 1200.0:
                        chain_snapshot[k] = v
                except (TypeError, ValueError):
                    pass
            if not chain_snapshot and nse_full:
                chain_snapshot = dict(nse_full)
            if chain_snapshot:
                price_source = "angel_ws+nse_chain"
                logger.debug("[LIVE] NIFTY chain from NSE fallback, strikes=%s", len(chain_snapshot))
        if not chain_snapshot:
            print("[SKIP] No live data", symbol, price, 0)
            _publish_waiting_state("waiting_option_chain")
            return
    if su == "SENSEX" and not chain_snapshot:
        print("[SKIP] No live data", symbol, price, 0)
        _publish_waiting_state("waiting_option_chain")
        return

    def _quick_oi(chain: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
        ce = 0.0
        pe = 0.0
        max_ce = (None, -1.0)  # strike, oi
        max_pe = (None, -1.0)
        for strike, sides in (chain or {}).items():
            ce_leg = sides.get("CE") if isinstance(sides, dict) else None
            pe_leg = sides.get("PE") if isinstance(sides, dict) else None
            if isinstance(ce_leg, dict):
                oi = float(ce_leg.get("oi") or 0.0)
                ce += oi
                if oi > max_ce[1]:
                    max_ce = (strike, oi)
            if isinstance(pe_leg, dict):
                oi = float(pe_leg.get("oi") or 0.0)
                pe += oi
                if oi > max_pe[1]:
                    max_pe = (strike, oi)
        pcr = pe / (ce + 1e-9) if (ce > 0 or pe > 0) else 0.0
        return {
            "pcr": pcr,
            "ce_oi_total": ce,
            "pe_oi_total": pe,
            "max_oi_call": float(max_ce[0]) if max_ce[0] is not None else None,
            "max_oi_put": float(max_pe[0]) if max_pe[0] is not None else None,
            "oi_spikes": [],
        }

    # Maintain short price history per symbol in process memory
    history = state.setdefault("_price_history", {})
    sym_hist = history.get(symbol) or []
    if price > 0:
        sym_hist.append(float(price))
        if len(sym_hist) > 128:
            sym_hist = sym_hist[-128:]
    history[symbol] = sym_hist

    features = compute_fast_features(chain_snapshot, float(price or 0.0), sym_hist)
    rule_signal = generate_fast_scalping_signal(features)
    ml_signal = generate_ml_signal(features)
    scalping_signal = rule_signal
    hero_zero = detect_hero_zero_fast(chain_snapshot, float(price or 0.0))
    confidence = compute_fast_confidence(features)
    call_oi = float(features.get("call_oi_strength") or 0.0)
    put_oi = float(features.get("put_oi_strength") or 0.0)
    call_vol = float(features.get("call_volume_strength") or 0.0)
    put_vol = float(features.get("put_volume_strength") or 0.0)
    mom = float(features.get("price_momentum") or 0.0)
    # ML fallback: if rule engine is flat, allow strong ML direction.
    if scalping_signal == "NO_TRADE" and ml_signal.get("label") in ("BUY_CE", "BUY_PE"):
        if int(ml_signal.get("confidence") or 0) >= 65:
            scalping_signal = str(ml_signal["label"])
            confidence = max(confidence, int(ml_signal["confidence"]) - 4)
    # SENSEX fallback when OI/volume is sparse: use ATM CE/PE premium imbalance + momentum.
    if su == "SENSEX" and scalping_signal == "NO_TRADE" and chain_snapshot:
        try:
            strikes = sorted(float(s) for s in chain_snapshot.keys())
            if strikes:
                atm = min(strikes, key=lambda s: abs(s - price))
                row = chain_snapshot.get(str(int(atm))) or chain_snapshot.get(str(atm)) or {}
                ce = row.get("CE") if isinstance(row, dict) else None
                pe = row.get("PE") if isinstance(row, dict) else None
                ce_ltp = float((ce or {}).get("ltp") or 0.0)
                pe_ltp = float((pe or {}).get("ltp") or 0.0)
                mom = float(features.get("price_momentum") or 0.0)
                if ce_ltp > 0 and pe_ltp > 0:
                    ratio = pe_ltp / max(1.0, ce_ltp)
                    if ratio >= 1.35 and mom <= 0.0002:
                        scalping_signal = "BUY_PE"
                        confidence = max(confidence, 62)
                    elif ratio <= 0.74 and mom >= -0.0002:
                        scalping_signal = "BUY_CE"
                        confidence = max(confidence, 62)
        except Exception:
            pass

    # High-confidence neutral tie-breaker:
    # when rule/ML end up NO_TRADE but feature bias is clearly one-sided, pick that side.
    if scalping_signal == "NO_TRADE":
        ce_bias = 0.45 * call_oi + 0.45 * call_vol + 0.10 * max(0.0, mom * 2000.0)
        pe_bias = 0.45 * put_oi + 0.45 * put_vol + 0.10 * max(0.0, -mom * 2000.0)
        bias_gap = abs(ce_bias - pe_bias)
        if confidence >= 70 and bias_gap >= 0.08:
            if ce_bias > pe_bias:
                scalping_signal = "BUY_CE"
            else:
                scalping_signal = "BUY_PE"
            confidence = max(confidence, 66 if su == "SENSEX" else 70)

    # Trend filter (fixed small window): block opposite-direction entries.
    trend = 0.0
    if len(sym_hist) >= 24:
        recent = sym_hist[-12:]
        prior = sym_hist[-24:-12]
        if prior:
            trend = (sum(recent) / len(recent)) - (sum(prior) / len(prior))
    trend_block = (6.0 if su == "SENSEX" else 2.5)
    if scalping_signal == "BUY_CE" and trend < -trend_block:
        scalping_signal = "NO_TRADE"
    elif scalping_signal == "BUY_PE" and trend > trend_block:
        scalping_signal = "NO_TRADE"

    # Volatility filter (fixed small window): avoid directional trades in flat tape.
    if len(sym_hist) >= 20 and scalping_signal in ("BUY_CE", "BUY_PE"):
        win = sym_hist[-20:]
        mid = max(1.0, float(price or 0.0))
        vol_pct = (max(win) - min(win)) / mid
        min_vol = 0.0012 if su == "SENSEX" else 0.0006
        if vol_pct < min_vol:
            scalping_signal = "NO_TRADE"

    # Side-balance dampener: reduce one-sided streaks with weak edge.
    bal_state = state.setdefault("_side_balance", {})
    sb = bal_state.setdefault(symbol, {"ce": 0.0, "pe": 0.0})
    sb["ce"] = float(sb.get("ce", 0.0)) * 0.97
    sb["pe"] = float(sb.get("pe", 0.0)) * 0.97
    if scalping_signal == "BUY_CE":
        sb["ce"] += 1.0
    elif scalping_signal == "BUY_PE":
        sb["pe"] += 1.0
    skew = sb["pe"] - sb["ce"]
    edge_gap = abs((call_oi + call_vol) - (put_oi + put_vol))
    if scalping_signal == "BUY_PE" and skew > 6.0 and edge_gap < 0.20:
        scalping_signal = "NO_TRADE"
    elif scalping_signal == "BUY_CE" and skew < -6.0 and edge_gap < 0.20:
        scalping_signal = "NO_TRADE"

    # Signal stabilizer + entry lock (actionable decision layer).
    MIN_CONF = 60 if su == "SENSEX" else 68
    STABLE_CYCLES = 2 if su == "SENSEX" else 4
    try:
        default_lock = "5.0" if symbol == "NIFTY" else "8.0"
        ENTRY_LOCK_SEC = float(os.getenv(f"ENTRY_LOCK_SEC_{su}", default_lock))
    except ValueError:
        ENTRY_LOCK_SEC = 5.0 if symbol == "NIFTY" else 8.0
    decision_state = state.setdefault("_decision_state", {})
    ds = decision_state.setdefault(
        symbol,
        {
            "last_signal": "NO_TRADE",
            "last_directional": None,
            "last_directional_ts": 0.0,
            "stable_count": 0,
            "lock_until": 0.0,
            "active_trade": None,
        },
    )
    now_sec = time.time()
    # Hysteresis: keep last directional signal briefly to avoid neutral flicker.
    try:
        hold_nifty = float(os.getenv("SIGNAL_HOLD_SEC_NIFTY", "10"))
    except ValueError:
        hold_nifty = 10.0
    try:
        hold_sensex = float(os.getenv("SIGNAL_HOLD_SEC_SENSEX", "12"))
    except ValueError:
        hold_sensex = 12.0
    HOLD_SEC = hold_sensex if su == "SENSEX" else hold_nifty
    if scalping_signal in ("BUY_CE", "BUY_PE"):
        ds["last_directional"] = scalping_signal
        ds["last_directional_ts"] = now_sec
    elif scalping_signal == "NO_TRADE":
        last_dir = ds.get("last_directional")
        last_ts = float(ds.get("last_directional_ts") or 0.0)
        if last_dir in ("BUY_CE", "BUY_PE") and (now_sec - last_ts) <= HOLD_SEC:
            scalping_signal = str(last_dir)
            confidence = max(confidence, 60 if su == "SENSEX" else 64)

    if scalping_signal in ("BUY_CE", "BUY_PE"):
        if ds.get("last_signal") == scalping_signal:
            ds["stable_count"] = int(ds.get("stable_count") or 0) + 1
        else:
            ds["stable_count"] = 1
    else:
        ds["stable_count"] = 0
    ds["last_signal"] = scalping_signal

    # Unlock previous trade once lock expires.
    if float(ds.get("lock_until") or 0.0) <= now_sec:
        ds["active_trade"] = None

    locked = float(ds.get("lock_until") or 0.0) > now_sec
    lock_remaining = max(0.0, float(ds.get("lock_until") or 0.0) - now_sec)
    stable_count = int(ds.get("stable_count") or 0)
    entry_decision = "HOLD"
    decision_reason = "no_trade_signal"
    if locked:
        prev_price = float(state["market"].get(symbol, {}).get("last_price") or price)
        lock_move = 6 if symbol == "NIFTY" else 20
        high_conf_unlock = (
            scalping_signal in ("BUY_CE", "BUY_PE")
            and confidence >= (82 if su == "NIFTY" else 78)
            and ds.get("last_signal") == scalping_signal
        )
        if abs(price - prev_price) >= lock_move or high_conf_unlock:
            entry_decision = scalping_signal
            decision_reason = "reentry_on_momentum" if not high_conf_unlock else "reentry_high_conf"
        else:
            decision_reason = f"entry_lock_active_{int(round(lock_remaining))}s"
    elif scalping_signal not in ("BUY_CE", "BUY_PE"):
        decision_reason = "signal_not_directional"
    elif confidence < MIN_CONF:
        decision_reason = f"low_confidence_{confidence}_lt_{MIN_CONF}"
    elif stable_count < STABLE_CYCLES:
        decision_reason = f"unstable_signal_{stable_count}_lt_{STABLE_CYCLES}"
    else:
        entry_decision = scalping_signal
        decision_reason = f"confirmed_{stable_count}_cycles_conf_{confidence}"
        ds["active_trade"] = scalping_signal
        ds["lock_until"] = now_sec + ENTRY_LOCK_SEC
        lock_remaining = ENTRY_LOCK_SEC

    # Derive strike + entry/target/stoploss from fast chain when we have a directional trade
    strike_label: Optional[str] = None
    entry: Optional[float] = None
    target: Optional[float] = None
    stoploss: Optional[float] = None

    def _select_fast_strike(
        chain: Dict[str, Dict[str, Dict[str, Any]]],
        ref_price: float,
        trade: str,
    ) -> Optional[Dict[str, Any]]:
        if not chain or ref_price <= 0:
            return None
        side = "CE" if trade == "BUY_CE" else "PE" if trade == "BUY_PE" else None
        if side is None:
            return None
        try:
            strikes = sorted(float(s) for s in chain.keys())
        except Exception:
            return None
        if not strikes:
            return None
        strike_step = abs(strikes[1] - strikes[0]) if len(strikes) > 1 else 50.0
        if strike_step <= 0:
            strike_step = 50.0
        atm = round(ref_price / strike_step) * strike_step

        # O(1) scan: fixed 5 strikes around ATM.
        candidates = [atm - 100.0, atm - 50.0, atm, atm + 50.0, atm + 100.0]
        best: Optional[Dict[str, Any]] = None
        best_score = -1.0

        for s_val in candidates:
            key = str(int(round(s_val)))
            row = chain.get(key) or {}
            leg = row.get(side) if isinstance(row, dict) else None
            if not isinstance(leg, dict):
                continue
            try:
                premium = float(leg.get("ltp"))
            except (TypeError, ValueError):
                continue
            if premium < 80.0 or premium > 250.0:
                continue
            vol = float(leg.get("volume") or 0.0)
            oi = float(leg.get("oi") or 0.0)
            score = vol + oi
            if score > best_score:
                best_score = score
                best = {"strike": s_val, "type": side, "premium": premium}

        if best is not None:
            return best

        # Fallback: pick ATM strike.
        atm_key = str(int(round(atm)))
        atm_row = chain.get(atm_key) or {}
        atm_leg = atm_row.get(side) if isinstance(atm_row, dict) else None
        if isinstance(atm_leg, dict):
            try:
                atm_premium = float(atm_leg.get("ltp"))
                return {"strike": atm, "type": side, "premium": atm_premium}
            except (TypeError, ValueError):
                pass
        return None

    if scalping_signal in ("BUY_CE", "BUY_PE"):
        sel = _select_fast_strike(chain_snapshot, float(price or 0.0), scalping_signal)
        if sel:
            s_val = sel["strike"]
            premium = sel["premium"]
            strike_label = f"{int(s_val)} {'CE' if scalping_signal == 'BUY_CE' else 'PE'}"
            entry = round(premium * 1.002, 2)
            target = round(premium * 1.4, 2)
            stoploss = round(premium * 0.8, 2)

    final_fast = {
        "symbol": symbol,
        "price": float(price or 0.0),
        "signal": scalping_signal,
        "confidence": confidence,
        "entry_decision": entry_decision,
        "decision_reason": decision_reason,
        "stable_count": stable_count,
        "lock_remaining_sec": int(round(lock_remaining)),
        "hero_zero": hero_zero,
        "strike": strike_label,
        "entry": entry,
        "target": target,
        "stoploss": stoploss,
        "price_source": price_source,
    }

    record_replay_tick(
        symbol,
        price,
        chain_snapshot,
        final_fast.get("signal"),
        final_fast.get("entry_decision"),
    )

    paper_trader.process_tick(symbol, final_fast, chain_snapshot, state)

    ts = datetime.now(timezone.utc)
    state["market"][symbol] = {
        "symbol": symbol,
        "last_price": float(price or 0.0),
        "ts": ts.isoformat(),
        "price_source": price_source,
    }
    state["fast_signals"][symbol] = final_fast
    # Populate legacy signal payload consumed by several API endpoints.
    state["signals"][symbol] = {
        "trade": scalping_signal,
        "confidence": confidence,
        "entry_decision": entry_decision,
        "decision_reason": decision_reason,
        "stable_count": stable_count,
        "lock_remaining_sec": int(round(lock_remaining)),
        "hero_zero": [hero_zero] if hero_zero else [],
        "oi_analysis": _quick_oi(chain_snapshot),
        "ml": ml_signal,
        "scalping": {
            "trade": scalping_signal,
            "confidence": confidence,
            "entry_decision": entry_decision,
            "decision_reason": decision_reason,
            "stable_count": stable_count,
            "strike": strike_label,
            "entry": entry,
            "target": target,
            "stoploss": stoploss,
        },
        "symbol": symbol,
        "price": float(price or 0.0),
    }

    # Backwards-compatible minimal final_signal for existing UI (trade/hero_zero/confidence)
    state["final_signal"][symbol] = {
        "symbol": symbol,
        "price": float(price or 0.0),
        "trade": scalping_signal,
        "confidence": confidence,
        "entry_decision": entry_decision,
        "decision_reason": decision_reason,
        "stable_count": stable_count,
        "lock_remaining_sec": int(round(lock_remaining)),
        "hero_zero": hero_zero,
        "entry": entry,
        "target": target,
        "stoploss": stoploss,
        "strike": strike_label,
        "regime": None,
        "gamma_wall": None,
        "max_pain": None,
        "institutional_flow": None,
    }

    # Conditional, rate-limited, async DB write:
    #   - only when signal != NO_TRADE OR confidence >= 60
    #   - at most once every 10 seconds per symbol
    #   - BUT always persist fresh directional entry decisions immediately
    if scalping_signal != "NO_TRADE" or confidence >= 60:
        cooldown_state = state.setdefault("_fast_store_cooldown", {})
        last_ts = cooldown_state.get(symbol)
        allow_write = True
        force_write = entry_decision in ("BUY_CE", "BUY_PE")
        if isinstance(last_ts, datetime):
            delta = (ts - last_ts).total_seconds()
            if delta < 10.0 and not force_write:
                allow_write = False
        if allow_write:
            cooldown_state[symbol] = ts
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.run_in_executor(None, _store_fast_signal_safe, symbol, ts, final_fast)
            else:
                _store_fast_signal_safe(symbol, ts, final_fast)

    # Always-on local signal recording (all states, including HOLD/NO_TRADE), throttled.
    # Directional entry decisions are always recorded immediately so they are never masked by lock-state HOLD rows.
    record_state = state.setdefault("_signal_record_cooldown", {})
    last_rec = float(record_state.get(symbol) or 0.0)
    now_ts = time.time()
    # Keep file size manageable while still being near real-time.
    force_record = entry_decision in ("BUY_CE", "BUY_PE")
    if force_record or (now_ts - last_rec) >= 2.0:
        record_state[symbol] = now_ts
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _append_signal_record_safe, symbol, ts, final_fast)
        else:
            _append_signal_record_safe(symbol, ts, final_fast)

    if _SIMULATION_MODE and _simulation_replay_window_open():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _append_replay_signal_record_safe, symbol, ts, final_fast, float(price or 0.0))
        else:
            _append_replay_signal_record_safe(symbol, ts, final_fast, float(price or 0.0))

    if scalping_signal != "NO_TRADE":
        logger.info("[FAST SIGNAL] %s price=%s confidence=%s", scalping_signal, price, confidence)



async def main_loop():
    while True:
        if _SIMULATION_MODE and not _simulation_replay_window_open():
            await asyncio.sleep(0.5)
            continue
        for symbol in settings.indices:
            try:
                await compute_for_symbol(symbol)
            except Exception as exc:
                logger.exception("Error in main loop for %s: %s", symbol, exc)
        if _SIMULATION_MODE and _SIMULATION_SPEED == "fast":
            continue
        await asyncio.sleep(0.5)  # low-latency cadence


def _persist_option_ticks():
    """Persist live option chain snapshot to option_ticks table (WS strike-keyed cache)."""
    try:
        from data.angel_option_stream import get_live_option_chain_snapshot

        snap = get_live_option_chain_snapshot()
        if not snap:
            return
        ticks = []
        sample = next(iter(snap.values()), None)
        if isinstance(sample, dict) and ("CE" in sample or "PE" in sample):
            for sides in snap.values():
                if not isinstance(sides, dict):
                    continue
                for side in ("CE", "PE"):
                    leg = sides.get(side)
                    if not isinstance(leg, dict) or not leg.get("token"):
                        continue
                    ticks.append(
                        {
                            "token": leg["token"],
                            "ltp": leg.get("ltp"),
                            "volume": leg.get("volume"),
                            "oi": leg.get("oi"),
                            "oi_change": leg.get("oi_change") or leg.get("change_oi"),
                        }
                    )
        else:
            for v in snap.values():
                if isinstance(v, dict) and v.get("token"):
                    ticks.append(
                        {
                            "token": v.get("token"),
                            "ltp": v.get("ltp"),
                            "volume": v.get("volume"),
                            "oi": v.get("oi"),
                            "oi_change": v.get("oi_change"),
                        }
                    )
        if ticks:
            insert_option_ticks("NIFTY", ticks)
    except Exception as e:
        logger.debug("[WS] Persist option ticks: %s", e)


@app.on_event("startup")
async def startup_event():
    global _scheduler
    logger.info("Initializing schema...")
    init_schema()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, bootstrap_historical_data)  # run in background, do not block

    # Scheduler for daily training and optional option-tick persist
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    # Keep daily trainer but it no longer affects live path
    _scheduler.add_job(
        train_models,
        trigger="cron",
        hour=2,
        minute=0,
        id="daily_model_training",
        replace_existing=True,
    )
    logger.info("[TRAINING] Daily trainer scheduled for 02:00 UTC")

    # WebSocket: OpenAPIScripMaster + get_nifty_option_tokens (OPTIDX, strike/100, ATM±range)
    try:
        import os

        from data.angel_instruments import load_instruments
        from data.angel_live_price import login_angel
        from data.angel_ws_manager import start_angel_ws_manager
        from data.nifty_option_tokens import get_nifty_option_tokens, get_sensex_option_tokens, refresh_master

        if os.getenv("ANGEL_REFRESH_INSTRUMENTS", "").strip() in ("1", "true", "yes"):
            refresh_master()
        login_angel()
        inst = load_instruments()
        nifty_spot = float(os.getenv("NIFTY_PROXY_SPOT", "23500"))
        nifty_atm = float(os.getenv("NIFTY_ATM_RANGE", "600"))
        nifty_max = int(os.getenv("NIFTY_MAX_WS_TOKENS", "240"))
        # Keep SENSEX proxy close to live; far proxy picks illiquid strikes.
        sensex_spot = float(os.getenv("SENSEX_PROXY_SPOT", "75000"))
        sensex_atm = float(os.getenv("SENSEX_ATM_RANGE", "800"))
        sensex_max = int(os.getenv("SENSEX_MAX_WS_TOKENS", "140"))

        nifty_exp = int(os.getenv("NIFTY_EXPIRY_COUNT", "2"))
        sensex_exp = int(os.getenv("SENSEX_EXPIRY_COUNT", "1"))
        n_toks, n_map = get_nifty_option_tokens(
            inst, nifty_spot, atm_range=nifty_atm, max_tokens=nifty_max, expiry_count=nifty_exp
        )
        s_toks, s_map = get_sensex_option_tokens(
            inst, sensex_spot, atm_range=sensex_atm, max_tokens=sensex_max, expiry_count=sensex_exp
        )
        all_map = {**n_map, **s_map}
        all_tokens = list(all_map.keys())
        print("[WS SUBSCRIBE] Option tokens:", len(all_tokens), all_tokens[:10])
        logger.info(
            "[WS] token split NIFTY=%s(exp=%s) SENSEX=%s(exp=%s)",
            len(n_toks),
            nifty_exp,
            len(s_toks),
            sensex_exp,
        )
        if not all_tokens:
            logger.error("[WS] STOP: 0 option tokens — raise NIFTY/SENSEX proxy spot or ATM ranges")
        if start_angel_ws_manager(None, option_token_map=all_map):
            logger.info("[WS] Started (option tokens=%s; NIFTY=%s SENSEX=%s)", len(all_tokens), len(n_toks), len(s_toks))
            if all_tokens:
                _scheduler.add_job(_persist_option_ticks, trigger="interval", seconds=60, id="persist_option_ticks", replace_existing=True)
        else:
            logger.warning("[WS] Manager not started — check ANGEL_* login and feed token")
    except Exception as exc:
        logger.warning("[WS] WebSocket startup skipped: %s", exc)

    if not _scheduler.running:
        _scheduler.start()

    asyncio.create_task(main_loop())
    logger.info("API server ready. Docs: http://localhost:8000/docs")


@app.get("/candles")
async def get_candles(symbol: str = "NIFTY", timeframe: str = "5m", limit: int = 200):
    """OHLC candles for chart (NIFTY/SENSEX). timeframe: 1m, 5m, 15m, 1d. Returns [{ time (unix), open, high, low, close }, ...]."""
    df = load_candles(symbol, timeframe, limit=min(limit, 500))
    if df.empty:
        return []
    df = df.sort_values("ts").reset_index(drop=True)
    out = []
    for _, row in df.iterrows():
        ts = row["ts"]
        unix = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(pd.Timestamp(ts).timestamp())
        out.append({
            "time": unix,
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
        })
    return out


@app.get("/market")
async def get_market():
    return {
        "market": state["market"],
        "model_version": state.get("model_version"),
    }


@app.get("/signals")
async def get_signals():
    return {
        "signals": state["signals"],
        "model_version": state.get("model_version"),
    }


@app.get("/scalping")
async def get_scalping():
    out = {}
    for symbol, sigs in state["signals"].items():
        out[symbol] = {
            "scalping": sigs.get("scalping"),
            "liquidity_sweep": sigs.get("liquidity_sweep"),
            "model_version": sigs.get("model_version"),
        }
    return out


@app.get("/hero-zero")
async def get_hero_zero():
    out = {}
    for symbol, sigs in state["signals"].items():
        out[symbol] = sigs.get("hero_zero")
    return out


@app.get("/institutional-flow")
async def get_institutional_flow():
    out = {}
    for symbol, sigs in state["signals"].items():
        out[symbol] = sigs.get("institutional_flow")
    return out


@app.get("/gamma")
async def get_gamma():
    out = {}
    for symbol, sigs in state["signals"].items():
        out[symbol] = sigs.get("gamma")
    return out


@app.get("/expiry")
async def get_expiry():
    out = {}
    for symbol, sigs in state["signals"].items():
        out[symbol] = sigs.get("expiry")
    return out


@app.get("/option-chain")
async def get_option_chain():
    """
    Live option chain from Angel WebSocket with analytics.
    Returns strike-keyed chain, PCR, max OI call/put, OI spikes, and hero-zero strikes for UI.
    """
    try:
        from data.angel_option_stream import get_all_live_option_chains, get_live_option_chain_snapshot
        from features.option_chain_builder import build_option_chain, option_chain_to_dataframe
        from features.oi_analysis import compute_oi_analysis
        chains = get_all_live_option_chains()
        snap = chains.get("NIFTY") or get_live_option_chain_snapshot("NIFTY")
        if not snap:
            from data.nse_option_chain import fetch_nifty_option_chain_nse, nse_fallback_enabled

            if nse_fallback_enabled():
                snap = fetch_nifty_option_chain_nse()
        # Chain: use strike-keyed snapshot as-is if it is strike-keyed, else build from flat
        first_key = next(iter(snap)) if snap else ""
        first_val = snap.get(first_key) if first_key else None
        is_strike_keyed = (
            isinstance(first_val, dict)
            and not (str(first_key).endswith("CE") or str(first_key).endswith("PE"))
            and ("CE" in first_val or "PE" in first_val)
        )
        chain = snap if is_strike_keyed else build_option_chain(snap)
        df = option_chain_to_dataframe(snap, symbol_prefix="NIFTY")
        analytics = compute_oi_analysis(df) if not df.empty else {}
        # Hero-zero strikes for dashboard highlighting
        hero_strikes = []
        for sym, sigs in state["signals"].items():
            for c in (sigs.get("hero_zero") or []):
                s = c.get("strike")
                if s is not None:
                    hero_strikes.append(float(s))
        sensex_snap = chains.get("SENSEX") or get_live_option_chain_snapshot("SENSEX")
        sx_chain = sensex_snap if sensex_snap else {}
        sx_df = option_chain_to_dataframe(sx_chain, symbol_prefix="SENSEX") if sx_chain else option_chain_to_dataframe({}, symbol_prefix="SENSEX")
        sx_analytics = compute_oi_analysis(sx_df) if not sx_df.empty else {}
        return {
            "NIFTY": {
                "chain": chain,
                "analytics": {
                    "pcr": analytics.get("pcr"),
                    "max_call_oi": analytics.get("max_oi_call"),
                    "max_put_oi": analytics.get("max_oi_put"),
                    "oi_spikes": analytics.get("oi_spikes", []),
                    "ce_oi_total": analytics.get("ce_oi_total"),
                    "pe_oi_total": analytics.get("pe_oi_total"),
                },
                "hero_zero_strikes": sorted(list(set(hero_strikes))),
            },
            "SENSEX": {
                "chain": sx_chain,
                "analytics": {
                    "pcr": sx_analytics.get("pcr"),
                    "max_call_oi": sx_analytics.get("max_oi_call"),
                    "max_put_oi": sx_analytics.get("max_oi_put"),
                    "oi_spikes": sx_analytics.get("oi_spikes", []),
                    "ce_oi_total": sx_analytics.get("ce_oi_total"),
                    "pe_oi_total": sx_analytics.get("pe_oi_total"),
                },
                "hero_zero_strikes": sorted(list(set(hero_strikes))),
            },
        }
    except Exception:
        return {
            "NIFTY": {"chain": {}, "analytics": {}, "hero_zero_strikes": []},
            "SENSEX": {"chain": {}, "analytics": {}, "hero_zero_strikes": []},
        }


@app.get("/ws-health")
async def get_ws_health():
    """
    WebSocket option-chain coverage by symbol.
    Useful to verify whether ltp/oi/volume/oi_change are flowing.
    """
    try:
        from data.angel_option_stream import get_all_live_option_chains
        from data.angel_ws_manager import get_ws_price

        chains = get_all_live_option_chains() or {}
        out: Dict[str, Any] = {}

        def _coverage(chain: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
            total_legs = 0
            ltp_legs = 0
            oi_legs = 0
            vol_legs = 0
            ch_legs = 0
            for sides in (chain or {}).values():
                if not isinstance(sides, dict):
                    continue
                for opt in ("CE", "PE"):
                    leg = sides.get(opt)
                    if not isinstance(leg, dict):
                        continue
                    total_legs += 1
                    if leg.get("ltp") is not None:
                        ltp_legs += 1
                    if leg.get("oi") is not None and float(leg.get("oi") or 0.0) > 0:
                        oi_legs += 1
                    if float(leg.get("volume") or 0.0) > 0:
                        vol_legs += 1
                    if leg.get("oi_change") is not None or leg.get("change_oi") is not None:
                        ch_legs += 1
            den = max(1, total_legs)
            return {
                "strikes": len(chain or {}),
                "legs": total_legs,
                "ltp_legs": ltp_legs,
                "oi_legs": oi_legs,
                "volume_legs": vol_legs,
                "oi_change_legs": ch_legs,
                "ltp_pct": round(100.0 * ltp_legs / den, 1),
                "oi_pct": round(100.0 * oi_legs / den, 1),
                "volume_pct": round(100.0 * vol_legs / den, 1),
                "oi_change_pct": round(100.0 * ch_legs / den, 1),
            }

        for sym in ("NIFTY", "SENSEX"):
            chain = chains.get(sym) or {}
            out[sym] = {
                "index_price": get_ws_price(sym),
                "coverage": _coverage(chain),
            }
        return {"ws_health": out}
    except Exception as exc:
        logger.debug("[WS HEALTH] failed: %s", exc)
        return {"ws_health": {"NIFTY": {}, "SENSEX": {}}, "error": str(exc)}


@app.get("/oi")
async def get_oi():
    """OI analysis (PCR, max OI strikes, OI spikes) per symbol."""
    out = {}
    for symbol, sigs in state["signals"].items():
        oi = sigs.get("oi_analysis")
        if oi:
            out[symbol] = oi
    return out


@app.get("/gamma-exposure")
async def get_gamma_exposure():
    """Dealer gamma exposure by strike, gamma walls, and gamma flip level."""
    out = {}
    for symbol, sigs in state["signals"].items():
        gex = sigs.get("gamma_levels")
        if isinstance(gex, dict) and (gex.get("gamma_levels") or gex.get("gamma_wall_call") is not None):
            out[symbol] = gex
    return out


@app.get("/max-pain")
async def get_max_pain():
    """Max pain strike per symbol."""
    out = {}
    for symbol, sigs in state["signals"].items():
        mp = sigs.get("max_pain")
        if mp is not None:
            out[symbol] = {"max_pain": mp}
    return out


@app.get("/expiry-bias")
async def get_expiry_bias():
    """Expiry direction bias and expected range per symbol."""
    out = {}
    for symbol, sigs in state["signals"].items():
        bias = sigs.get("expiry_bias")
        rng = sigs.get("expiry_bias_range")
        if bias is not None or rng:
            out[symbol] = {"expiry_bias": bias, "expected_range": rng or []}
    return out


@app.get("/market-regime")
async def get_market_regime():
    """Market regime (TREND_UP, TREND_DOWN, RANGE, VOLATILE) and confidence per symbol."""
    return {"market_regime": state.get("market_regime", {})}


@app.get("/liquidity-map")
async def get_liquidity_map():
    """Liquidity heatmap, support/resistance zones, stop-loss clusters per symbol."""
    return {"liquidity_map": state.get("liquidity_map", {})}


@app.get("/stop-hunts")
async def get_stop_hunts():
    """Stop-hunt (liquidity grab) detection per symbol."""
    return {"stop_hunts": state.get("stop_hunts", {})}


@app.get("/final-signal")
async def get_final_signal():
    """Final combined signal per symbol (Section 19 format)."""
    return {"final_signal": state.get("final_signal", {})}


@app.get("/paper-trades")
async def get_paper_trades():
    """Paper-trading dashboard payload (active legs, stats, last trade, guide)."""
    return {"paper_trades": state.get("paper_trades", {"active": {}, "stats": {}, "last_trade": None, "guide": {}})}


@app.get("/signal-history")
async def get_signal_history_endpoint(symbol: str = "NIFTY", limit: int = 50):
    """History of signals given by the system (final signal per tick). Newest first."""
    history = get_signal_history(symbol=symbol, category="final", limit=min(limit, 200))
    return {"symbol": symbol, "history": history}


@app.websocket("/ws/signals")
async def websocket_signals(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json({
                "fast": state.get("fast_signals", {}),
            })
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")


@app.websocket("/ws/fast")
async def websocket_fast(ws: WebSocket):
    """
    Lightweight, low-latency WebSocket endpoint.

    Sends only: symbol, price, signal, confidence, hero_zero
    for each symbol, every 300–500ms. Payload is event-like: if there is no
    fast signal yet, the map may be empty.
    """
    await ws.accept()
    last_payload: Dict[str, Any] | None = None
    try:
        while True:
            fast = state.get("fast_signals", {})
            # Strip to required fields only
            slim: Dict[str, Any] = {}
            for sym, sig in fast.items():
                if not isinstance(sig, dict):
                    continue
                slim[sym] = {
                    "symbol": sym,
                    "price": sig.get("price"),
                    "signal": sig.get("signal"),
                    "confidence": sig.get("confidence"),
                    "hero_zero": sig.get("hero_zero"),
                }
            # Prefer event-driven push: only send when payload changes
            if slim != last_payload:
                await ws.send_json(slim)
                last_payload = slim
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        logger.info("Fast WebSocket client disconnected")

