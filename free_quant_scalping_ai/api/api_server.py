from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

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
    reset_final_fast_signal_history,
    store_signal,
)
from data.historical_fetcher import bootstrap_historical_data
from app.services import execution_broker, option_chain_service, signal_engine
from app.services.market_calendar import (
    get_market_status as calendar_market_status,
    is_market_open as calendar_is_market_open,
)
from app.services.market_intelligence import build_market_intelligence
from app.services.strategy_manager import get_strategy_config
from app.services.mode_manager import apply_profile, detect_mode
from app.services.websocket_service import get_feed_health_snapshot, get_index_ltp, get_option_chain_snapshot
from engines.platform_runner import run_engine_tick
from engines.slow_engine_runner import slow_engine_loop
from engines.strategy_signal_runner import strategy_signal_loop
from engines.trade_lifecycle import ExecutionLifecycle, parse_strike_label
from training.daily_trainer import train_models
from utils.file_logs import attach_error_file_handler
from utils.logger import get_logger
from utils.runtime_stability import (
    load_persisted_execution_state,
    mark_startup,
    maybe_persist_execution_state,
    maybe_throttled_skip_log,
    merge_skip_reason,
    record_successful_compute_tick,
    stability_entry_skip_reason,
)

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


class SensexSessionBody(BaseModel):
    enabled: bool


state: Dict[str, Any] = {
    "market": {},
    "signals": {},
    "final_signal": {},
    "fast_signals": {},
    "engine_platform": {},
    "market_regime": {},
    "liquidity_map": {},
    "stop_hunts": {},
    "model_version": None,
    "execution_final_signal": {},
    "execution_opportunity_signal": {},
    "engine_signals": {},
    "strategy_signals": {},
    "latency": {},
    "data_status": {},
    "market_status": {},
    "system_mode": {},
    "market_intelligence": {},
    "strategy_config": {},
    # SENSEX live path: default off (NIFTY-only compute + optional NIFTY-only WS tokens).
    "sensex_session_enabled": (os.getenv("SENSEX_SESSION_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")),
    "_ws_subscribed_sensex_options": False,
    # Isolated expiry experiment (engines/hero_zero_expiry_engine.py); not used by execution/scalping.
    "hero_zero_expiry": {},
}

_execution_lifecycle = ExecutionLifecycle()

_scheduler: BackgroundScheduler | None = None
_IST = timezone(timedelta(hours=5, minutes=30))


def active_compute_symbols() -> List[str]:
    """Symbols included in the live main loop (ACTIVE_SYMBOLS env > session toggle fallback)."""
    raw = str(os.getenv("ACTIVE_SYMBOLS", "") or "").strip().upper()
    if raw:
        toks = [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]
        allowed = [s for s in toks if s in ("NIFTY", "SENSEX")]
        if allowed:
            out: List[str] = []
            for s in allowed:
                if s not in out:
                    out.append(s)
            return out
    if state.get("sensex_session_enabled"):
        return ["NIFTY", "SENSEX"]
    return ["NIFTY"]


def is_market_open(now_utc: Optional[datetime] = None) -> bool:
    return calendar_is_market_open(now_utc)


def _clear_sensex_runtime_state() -> None:
    """Drop SENSEX rows from in-memory live state when session disables SENSEX."""
    sym = "SENSEX"
    for key in ("market", "signals", "fast_signals", "final_signal", "engine_platform", "market_regime"):
        bucket = state.get(key)
        if isinstance(bucket, dict) and sym in bucket:
            del bucket[sym]
    ph = state.get("_price_history")
    if isinstance(ph, dict) and sym in ph:
        del ph[sym]
    et = state.get("_execution_trade")
    if isinstance(et, dict) and sym in et:
        del et[sym]
    ef = state.get("execution_final_signal")
    if isinstance(ef, dict) and sym in ef:
        del ef[sym]
    eo = state.get("execution_opportunity_signal")
    if isinstance(eo, dict) and sym in eo:
        del eo[sym]
    ds = state.get("_decision_state")
    if isinstance(ds, dict) and sym in ds:
        del ds[sym]
    sb = state.get("_side_balance")
    if isinstance(sb, dict) and sym in sb:
        del sb[sym]
    pt = state.get("paper_trades")
    if isinstance(pt, dict):
        act = pt.get("active")
        if isinstance(act, dict) and sym in act:
            del act[sym]
    lt = state.get("_last_telegram_signal")
    if isinstance(lt, dict) and sym in lt:
        del lt[sym]
    es = state.get("engine_signals")
    if isinstance(es, dict) and sym in es:
        del es[sym]
    ss = state.get("strategy_signals")
    if isinstance(ss, dict) and sym in ss:
        del ss[sym]


def _discovery_index_spot(symbol: str, proxy_env: str, default: float) -> float:
    """
    Option token subscription uses a spot estimate before WS index ticks exist.
    If NIFTY_PROXY_SPOT (etc.) is stale vs Yahoo last close, strikes can sit far from live ATM
    so chain LTP / entry / SL look wrong. When Yahoo is >1.5% away from env proxy, prefer Yahoo.
    Set DISABLE_YAHOO_SPOT_FOR_DISCOVERY=1 to always use env proxy only.
    """
    try:
        proxy = float(os.getenv(proxy_env, str(default)))
    except ValueError:
        proxy = default
    if proxy <= 0:
        proxy = default
    if (os.getenv("DISABLE_YAHOO_SPOT_FOR_DISCOVERY", "").strip().lower() in ("1", "true", "yes", "on")):
        return proxy
    ysym = settings.yahoo_symbols.get(str(symbol).upper())
    if not ysym:
        return proxy
    try:
        import yfinance as yf

        h = yf.Ticker(ysym).history(period="5d", interval="1d", auto_adjust=False, progress=False)
        if h is None or h.empty:
            return proxy
        last = float(h["Close"].iloc[-1])
        if last <= 0:
            return proxy
        denom = max(abs(proxy), 1.0)
        if abs(last - proxy) / denom > 0.015:
            logger.info(
                "[WS] %s discovery spot: Yahoo %.2f (env %s=%.2f) — using Yahoo for ATM token band",
                symbol.upper(),
                last,
                proxy_env,
                proxy,
            )
            return last
    except Exception as exc:
        logger.debug("[WS] Yahoo discovery spot for %s failed: %s", symbol, exc)
    return proxy


_SIGNAL_LOG_PATH = Path(os.getenv("SIGNAL_RECORD_FILE", "logs/signal_events.jsonl"))
_AUDIT_LATENCY_PATH = Path(os.getenv("AUDIT_LATENCY_LOG", "logs/audit_latency.jsonl"))


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


def _execution_entry_skip_reason(sym_hist: List[float], price: float) -> Optional[str]:
    """
    Optional entry gates: tight index range (chop) and weak short-horizon momentum.
    Returns a machine-readable reason string, or None when entry is not blocked by these gates.
    """
    if len(sym_hist) < 8:
        return None
    try:
        min_move = float(os.getenv("EXEC_MIN_PRICE_MOVE", "2.5"))
    except ValueError:
        min_move = 2.5
    try:
        lookback = int(os.getenv("EXEC_NO_TRADE_RANGE_LOOKBACK", "20"))
    except ValueError:
        lookback = 20
    try:
        max_range = float(os.getenv("EXEC_NO_TRADE_MAX_INDEX_RANGE", "5.0"))
    except ValueError:
        max_range = 5.0
    lookback = max(3, min(lookback, len(sym_hist)))
    window = sym_hist[-lookback:]
    hi = max(window)
    lo = min(window)
    if (hi - lo) < max_range:
        return f"no_trade_choppy_range_{hi - lo:.2f}_lt_{max_range}"
    if len(sym_hist) >= 6:
        ref = sym_hist[-6]
        if abs(float(price) - float(ref)) < min_move:
            return f"no_trade_low_momentum_{abs(float(price) - float(ref)):.2f}_lt_{min_move}"
    return None


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
            "call_oi_strength": payload.get("call_oi_strength"),
            "put_oi_strength": payload.get("put_oi_strength"),
            "call_volume_strength": payload.get("call_volume_strength"),
            "put_volume_strength": payload.get("put_volume_strength"),
            "price_momentum": payload.get("price_momentum"),
        }
        for k in (
            "audit_aggregate_signal",
            "audit_aggregate_confidence",
            "audit_risk",
            "audit_regime",
            "audit_skip_entry_reason",
        ):
            if k in payload:
                row[k] = payload.get(k)
        with _SIGNAL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
    except Exception as exc:
        logger.debug("Failed to append signal record for %s: %s", symbol, exc)


def _append_audit_latency_row(symbol: str, t0: float, index_price: float) -> None:
    """Full-tick latency (JSONL). Enable with AUDIT_LATENCY_JSONL=1."""
    try:
        _AUDIT_LATENCY_PATH.parent.mkdir(parents=True, exist_ok=True)
        health = get_feed_health_snapshot()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "compute_ms": round(dt_ms, 3),
            "index_price": float(index_price or 0.0),
            "last_index_tick_age_sec": health.get("last_index_tick_age_sec"),
            "last_any_tick_age_sec": health.get("last_any_tick_age_sec"),
        }
        with _AUDIT_LATENCY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
    except Exception as exc:
        logger.debug("audit latency append failed: %s", exc)


def _update_realtime_latency(symbol: str, t0: float, ws_age_sec: Optional[float]) -> None:
    """State-only latency meter for dashboard monitoring."""
    compute_ms = max(0.0, (time.perf_counter() - t0) * 1000.0)
    ws_latency_ms = float(ws_age_sec) * 1000.0 if ws_age_sec is not None else 0.0
    total_latency_ms = compute_ms + ws_latency_ms
    now_iso = datetime.now(timezone.utc).isoformat()
    bucket = state.setdefault("latency", {})
    prev = bucket.get(symbol) if isinstance(bucket, dict) else None
    n_prev = int((prev or {}).get("sample_count") or 0)
    avg_prev = float((prev or {}).get("rolling_avg_ms") or total_latency_ms)
    n_now = n_prev + 1
    rolling_avg = ((avg_prev * n_prev) + total_latency_ms) / max(1, n_now)
    max_latency = max(float((prev or {}).get("max_latency_ms") or 0.0), total_latency_ms)
    bucket[symbol] = {
        "compute_ms": round(compute_ms, 3),
        "ws_latency_ms": round(ws_latency_ms, 3),
        "total_latency_ms": round(total_latency_ms, 3),
        "rolling_avg_ms": round(rolling_avg, 3),
        "max_latency_ms": round(max_latency, 3),
        "sample_count": int(n_now),
        "timestamp": now_iso,
    }


def _publish_waiting_signal_state(symbol: str, reason: str) -> None:
    """No index / chain / stale feed: full scalping-shaped payload so /signals and UI never see an empty row."""
    ts = datetime.now(timezone.utc)
    try:
        age = float((get_feed_health_snapshot() or {}).get("last_index_tick_age_sec") or 9999.0)
    except (TypeError, ValueError):
        age = 9999.0
    _set_data_status(symbol, False, age)
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
            "chain_ltp": None,
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
        "chain_ltp": None,
        "chain_ltp_age_sec": None,
        "option_expiry": None,
        "regime": None,
        "gamma_wall": None,
        "max_pain": None,
        "institutional_flow": None,
    }
    if str(symbol).upper() == "NIFTY":
        from engines.hero_zero_expiry_engine import default_no_trade_result

        state.setdefault("hero_zero_expiry", {})["NIFTY"] = default_no_trade_result(time.time(), reason)


def _build_opportunity_signal(
    *,
    symbol: str,
    signal: str,
    strike: Optional[str],
    entry: Optional[float],
    target: Optional[float],
    stoploss: Optional[float],
    confidence: float,
    reason: str,
    index_price: float,
) -> Dict[str, Any]:
    return {
        "status": "OPPORTUNITY",
        "type": "OPPORTUNITY",
        "stage": "ENTRY",
        "symbol": symbol,
        "signal": signal,
        "strike": strike,
        "entry": entry,
        "ltp": entry,
        "target": target,
        "sl": stoploss,
        "trailing_sl": stoploss,
        "pnl": 0.0,
        "qty": None,
        "confidence": float(confidence),
        "reason": reason,
        "index_price": float(index_price or 0.0),
        "time": datetime.now(timezone.utc).isoformat(),
    }


def _set_data_status(symbol: str, is_fresh: bool, age_sec: float) -> None:
    state.setdefault("data_status", {})[symbol] = {
        "is_fresh": bool(is_fresh),
        "age_sec": round(float(max(0.0, age_sec)), 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _refresh_market_status_state() -> Dict[str, Any]:
    syms = active_compute_symbols()
    by_symbol: Dict[str, Any] = {s: calendar_market_status(s) for s in syms}
    primary = by_symbol.get("NIFTY") or (by_symbol[syms[0]] if syms else calendar_market_status("NIFTY"))
    merged = {
        **primary,
        "active_symbols": syms,
        "by_symbol": by_symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    state["market_status"] = merged
    return merged


def _apply_strategy_levels(
    entry: Optional[float],
    target: Optional[float],
    stoploss: Optional[float],
    strategy_cfg: Dict[str, Any],
) -> tuple[Optional[float], Optional[float]]:
    if entry is None:
        return target, stoploss
    ent = float(entry)
    tgt = float(target) if target is not None else None
    sl = float(stoploss) if stoploss is not None else None
    t_mult = float(strategy_cfg.get("target_multiplier") or 1.0)
    s_mult = float(strategy_cfg.get("stop_multiplier") or 1.0)
    if tgt is not None:
        span = max(0.0, tgt - ent)
        tgt = round(ent + span * t_mult, 2)
    if sl is not None:
        risk = max(0.0, ent - sl)
        sl = round(ent - risk * s_mult, 2)
    return tgt, sl


def _apply_detected_mode(reason: str = "startup") -> Dict[str, Any]:
    mode = detect_mode("NIFTY")
    applied = apply_profile(mode)
    out = {
        "mode": mode,
        "reason": reason,
        "profile": applied,
        "market_status": state.get("market_status") or _refresh_market_status_state(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    state["system_mode"] = out
    return out


async def mode_watch_loop() -> None:
    interval = max(20.0, float(os.getenv("MODE_CHECK_SEC", "60")))
    last_mode = str((state.get("system_mode") or {}).get("mode") or "")
    while True:
        try:
            _refresh_market_status_state()
            next_mode = detect_mode("NIFTY")
            if next_mode != last_mode:
                res = _apply_detected_mode("auto_mode_change")
                logger.info("[MODE] switched %s -> %s profile=%s", last_mode or "-", next_mode, res.get("profile", {}).get("resolved_profile_file"))
                last_mode = next_mode
        except Exception as exc:
            logger.debug("mode watch failed: %s", exc)
        await asyncio.sleep(interval)


def _maybe_log_market_transition(is_open: bool) -> None:
    prev = state.get("_market_open_last")
    state["_market_open_last"] = bool(is_open)
    if prev is None:
        return
    if bool(prev) != bool(is_open):
        logger.info("[MARKET] transition -> %s", "OPEN" if is_open else "CLOSED")


def _compute_for_symbol_impl(symbol: str) -> None:
    """
    High-performance live path (runs in a thread pool so FastAPI's event loop stays free):
      1) read latest Angel live price
      2) read fast in-memory option chain snapshot (no pandas)
      3) compute fast features
      4) generate scalping + hero-zero signals
      5) compute lightweight confidence
      6) update fast state and (optionally) persist
    """
    t_compute0 = time.perf_counter()
    su = symbol.upper()
    now_epoch = time.time()
    ms = _refresh_market_status_state()
    mkt_open = bool(ms.get("is_open"))
    _maybe_log_market_transition(mkt_open)
    if not mkt_open:
        health0 = get_feed_health_snapshot() or {}
        idx_age0 = health0.get("last_index_tick_age_sec")
        try:
            age0 = float(idx_age0) if idx_age0 is not None else 9999.0
        except (TypeError, ValueError):
            age0 = 9999.0
        _set_data_status(symbol, False, age0)
        if bool(ms.get("is_holiday")):
            _publish_waiting_signal_state(symbol, f"holiday_closed:{ms.get('holiday_name') or 'holiday'}")
        else:
            _publish_waiting_signal_state(symbol, "market_closed")
        return

    price = get_index_ltp(su)
    chain_snapshot = get_option_chain_snapshot(su)
    if not price or float(price) <= 0:
        _publish_waiting_signal_state(symbol, "waiting_index_price")
        return
    price = float(price)
    health = get_feed_health_snapshot()
    try:
        max_age = float(os.getenv("MAX_INDEX_TICK_AGE_SEC", "42"))
    except ValueError:
        max_age = 42.0
    idx_age = health.get("last_index_tick_age_sec")
    try:
        stale_guard_sec = float(os.getenv("SIGNAL_STALE_DATA_SEC", "1.5"))
    except ValueError:
        stale_guard_sec = 1.5
    if idx_age is None:
        _set_data_status(symbol, False, 9999.0)
    else:
        _set_data_status(symbol, float(idx_age) <= stale_guard_sec, float(idx_age))
    if idx_age is not None and float(idx_age) > stale_guard_sec:
        stale_state = state.setdefault("_stale_data_last_log_ts", {})
        last_log = float(stale_state.get(symbol) or 0.0)
        if (now_epoch - last_log) >= 2.0:
            stale_state[symbol] = now_epoch
            logger.warning(
                "[STALE DATA] %s age=%.2fs > %.2fs — signals disabled",
                symbol,
                float(idx_age),
                stale_guard_sec,
            )
        _publish_waiting_signal_state(symbol, f"stale_data_{float(idx_age):.2f}s_gt_{stale_guard_sec:.2f}s")
        return
    if idx_age is not None and idx_age > max_age:
        _publish_waiting_signal_state(symbol, f"stale_index_feed_{int(idx_age)}s")
        return
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
            _publish_waiting_signal_state(symbol, "waiting_option_chain")
            return
    if su == "SENSEX" and not chain_snapshot:
        print("[SKIP] No live data", symbol, price, 0)
        _publish_waiting_signal_state(symbol, "waiting_option_chain")
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
    mstat_by_symbol = ((state.get("market_status") or {}).get("by_symbol") or {}).get(su)
    market_intel = build_market_intelligence(symbol, sym_hist, mstat_by_symbol if isinstance(mstat_by_symbol, dict) else None)
    strategy_cfg = get_strategy_config(market_intel)
    state.setdefault("market_intelligence", {})[symbol] = market_intel
    state.setdefault("strategy_config", {})[symbol] = strategy_cfg

    from engines.hero_zero_expiry_engine import run_hero_zero_expiry_engine

    state.setdefault("hero_zero_expiry", {})[su] = run_hero_zero_expiry_engine(
        state, symbol, price, chain_snapshot, time.time()
    )

    oi_snap = _quick_oi(chain_snapshot)
    platform = run_engine_tick(
        state,
        symbol=symbol,
        price=float(price or 0.0),
        chain_snapshot=chain_snapshot,
        sym_hist=sym_hist,
        oi_snap=oi_snap,
    )
    sp = platform["scalping_pipeline"]
    state.setdefault("engine_platform", {})[symbol] = {
        "engines": platform["engines"],
        "aggregate": platform["aggregate"],
        "aggregate_raw": platform.get("aggregate_raw"),
        "risk": platform["risk"],
        "regime": platform.get("regime"),
        "support_resistance": platform.get("support_resistance"),
    }
    state.setdefault("market_regime", {})[symbol] = platform.get("regime")

    scalping_signal = str(sp.get("scalping_signal") or "NO_TRADE")
    confidence = int(sp.get("confidence") or 0)
    ml_signal = sp.get("ml_signal") or {"label": "NO_TRADE", "confidence": 0}
    hero_zero = sp.get("hero_zero")
    features = sp.get("features") or {}
    debug: List[str] = list(sp.get("debug") or [])
    call_oi = float(sp.get("call_oi") or 0.0)
    put_oi = float(sp.get("put_oi") or 0.0)
    call_vol = float(sp.get("call_vol") or 0.0)
    put_vol = float(sp.get("put_vol") or 0.0)
    mom = float(sp.get("mom") or 0.0)
    entry_decision = str(sp.get("entry_decision") or "HOLD")
    decision_reason = str(sp.get("decision_reason") or "")
    stable_count = int(sp.get("stable_count") or 0)
    lock_remaining = float(sp.get("lock_remaining") or 0.0)
    vol_fb = bool(sp.get("volume_fallback_used") or features.get("volume_fallback_used"))
    vol_avail = bool(sp.get("volume_available", features.get("volume_available", True)))
    if signal_engine.SIGNAL_DEBUG:
        mc, scy, elock = signal_engine.stabilizer_params(su, symbol)
        logger.info(
            "[SIGNAL_DEBUG] %s sig=%s conf=%s entry=%s reason=%s | mom=%.5f coi=%.2f poi=%.2f cvol=%.2f pvol=%.2f | "
            "volume_available=%s volume_fallback_used=%s | debug=%s | thresholds min_conf=%s stable=%s lock=%.1fs",
            symbol,
            scalping_signal,
            confidence,
            entry_decision,
            decision_reason,
            mom,
            call_oi,
            put_oi,
            call_vol,
            put_vol,
            vol_avail,
            vol_fb,
            ";".join(debug) or "-",
            mc,
            scy,
            elock,
        )

    strike_label: Optional[str] = None
    chain_ltp: Optional[float] = None
    chain_ltp_age_sec: Optional[float] = None
    entry: Optional[float] = None
    target: Optional[float] = None
    stoploss: Optional[float] = None

    def _leg_levels_for_bias(
        bias: str,
    ) -> tuple[
        Optional[str],
        Optional[float],
        Optional[float],
        Optional[float],
        Optional[float],
        Optional[float],
        Optional[str],
    ]:
        b = str(bias or "").strip().upper()
        if b not in ("BUY_CE", "BUY_PE"):
            return None, None, None, None, None, None, None
        use_win = os.getenv("EXEC_USE_ATM_WINDOW", "1").strip().lower() in ("1", "true", "yes")
        prev_map = (state.get("_leg_ltp_history") or {}).get(symbol) or {}
        try:
            back = int(os.getenv("EXEC_ATM_STEPS_BACK", "5"))
            fwd = int(os.getenv("EXEC_ATM_STEPS_FWD", "2"))
        except ValueError:
            back, fwd = 5, 2
        if use_win:
            sel = option_chain_service.select_strike_atm_window(
                chain_snapshot,
                float(price or 0.0),
                b,
                atm_steps_back=back,
                atm_steps_fwd=fwd,
                ltp_prev_map=prev_map if isinstance(prev_map, dict) else None,
            )
        else:
            sel = option_chain_service.select_strike_for_scalp(chain_snapshot, float(price or 0.0), b)
        if not sel:
            return None, None, None, None, None, None, None
        s_val = sel["strike"]
        side_leg = "CE" if b == "BUY_CE" else "PE"
        row = option_chain_service.chain_row_for_strike(chain_snapshot, float(s_val))
        leg = row.get(side_leg) if isinstance(row, dict) else None
        premium = option_chain_service.option_leg_last_price(leg) if leg else None
        if premium is None:
            try:
                premium = float(sel.get("premium") or 0.0)
            except (TypeError, ValueError):
                premium = None
        if premium is None:
            return None, None, None, None, None, None, None
        prem_f = float(premium)
        hist = state.setdefault("_leg_ltp_history", {}).setdefault(symbol, {})
        hist[f"{int(s_val)}_{side_leg}"] = prem_f
        cltp = round(prem_f, 4)
        slabel = f"{int(s_val)} {side_leg}"
        ent = round(prem_f, 2)
        leg_ts = leg.get("ts") if isinstance(leg, dict) else None
        age_sec: Optional[float] = None
        if leg_ts is not None:
            try:
                age_sec = round(max(0.0, time.time() - float(leg_ts)), 2)
            except (TypeError, ValueError):
                age_sec = None
        opt_exp = (
            str(leg.get("expiry") or "").strip()
            if isinstance(leg, dict) and leg.get("expiry")
            else None
        )
        return slabel, cltp, ent, round(prem_f * 1.4, 2), round(prem_f * 0.8, 2), age_sec, opt_exp

    strike_label, chain_ltp, entry, target, stoploss, chain_ltp_age_sec, option_expiry = _leg_levels_for_bias(
        scalping_signal
    )
    target, stoploss = _apply_strategy_levels(entry, target, stoploss, strategy_cfg)

    agg = platform.get("aggregate") or {}
    agg_sig = str(agg.get("signal") or "NO_TRADE").strip().upper()
    (
        agg_strike,
        agg_ltp,
        agg_entry,
        agg_tgt,
        agg_sl,
        agg_ltp_age_sec,
        agg_option_expiry,
    ) = _leg_levels_for_bias(agg_sig)
    agg_tgt, agg_sl = _apply_strategy_levels(agg_entry, agg_tgt, agg_sl, strategy_cfg)

    aggregate_leg_fields: Dict[str, Any] = {}
    if agg_sig in ("BUY_CE", "BUY_PE") and agg_strike:
        aggregate_leg_fields = {
            "aggregate_leg_signal": agg_sig,
            "aggregate_leg_strike": agg_strike,
            "aggregate_leg_chain_ltp": agg_ltp,
            "aggregate_leg_chain_ltp_age_sec": agg_ltp_age_sec,
            "aggregate_leg_entry": agg_entry,
            "aggregate_leg_target": agg_tgt,
            "aggregate_leg_stoploss": agg_sl,
            "aggregate_leg_expiry": agg_option_expiry,
        }

    final_fast: Dict[str, Any] = {
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
        "chain_ltp": chain_ltp,
        "chain_ltp_age_sec": chain_ltp_age_sec,
        "entry": entry,
        "target": target,
        "stoploss": stoploss,
        "option_expiry": option_expiry,
        # Premium levels: entry = chain LTP at selected strike; target/stop = entry×1.4 / entry×0.8 (api_server _leg_levels_for_bias)
        "premium_target_mult": 1.4,
        "premium_stop_mult": 0.8,
        "levels_basis": "option_chain_leg_ltp_at_signal_strike",
        **aggregate_leg_fields,
        "price_source": price_source,
        "call_oi_strength": call_oi,
        "put_oi_strength": put_oi,
        "call_volume_strength": call_vol,
        "put_volume_strength": put_vol,
        "price_momentum": mom,
        "volume_available": vol_avail,
        "volume_fallback_used": vol_fb,
        "intent": (platform.get("aggregate") or {}).get("intent"),
    }
    ep = state.get("engine_platform", {}).get(symbol) or {}
    final_fast["platform_aggregate"] = ep.get("aggregate")
    final_fast["platform_aggregate_raw"] = ep.get("aggregate_raw")
    final_fast["platform_risk"] = ep.get("risk")
    if signal_engine.SIGNAL_DEBUG:
        final_fast["signal_debug"] = signal_engine.format_debug_payload(
            debug,
            features,
            {
                "min_conf_nifty": os.getenv("MIN_CONF_NIFTY", "62"),
                "min_conf_sensex": os.getenv("MIN_CONF_SENSEX", "58"),
                "trend_block_nifty": os.getenv("TREND_BLOCK_POINTS_NIFTY", "10"),
                "trend_block_sensex": os.getenv("TREND_BLOCK_POINTS_SENSEX", "30"),
            },
        )

    agg_conf_f = float((agg.get("confidence") or 0.0))
    try:
        agg_conf_f = max(0.0, min(100.0, agg_conf_f * float(strategy_cfg.get("aggregate_confidence_multiplier") or 1.0)))
    except (TypeError, ValueError):
        pass
    strike_num_parsed, opt_parsed = parse_strike_label(strike_label)
    opt_for_exec = opt_parsed or (
        "CE" if str(scalping_signal).upper() == "BUY_CE" else "PE" if str(scalping_signal).upper() == "BUY_PE" else None
    )
    prev_alert = (state.get("execution_final_signal") or {}).get(symbol)
    record_successful_compute_tick(state, symbol)
    skip_chop = _execution_entry_skip_reason(sym_hist, float(price or 0.0))
    skip_rt = stability_entry_skip_reason(
        state,
        symbol,
        confidence=float(confidence),
        min_entry_confidence=float(_execution_lifecycle.min_conf),
    )
    skip_entry = merge_skip_reason(skip_rt, skip_chop)
    maybe_throttled_skip_log(state, symbol, skip_entry)
    if skip_entry and os.getenv("SIGNAL_FILTER_LOG", "1").strip().lower() in ("1", "true", "yes", "on"):
        logger.debug("[SIGNAL_FILTER] %s filter_block|execution_skip|%s", symbol, skip_entry)
    if os.getenv("SIGNAL_RECORD_EXTENDED", "1").strip().lower() in ("1", "true", "yes", "on"):
        reg = platform.get("regime")
        reg_name = reg.get("regime") if isinstance(reg, dict) else reg
        final_fast["audit_aggregate_signal"] = agg_sig
        final_fast["audit_aggregate_confidence"] = round(agg_conf_f, 2)
        final_fast["audit_risk"] = platform.get("risk")
        final_fast["audit_regime"] = reg_name
        final_fast["audit_skip_entry_reason"] = skip_entry
    _execution_lifecycle.process_tick(
        state,
        symbol,
        index_price=float(price or 0.0),
        chain_snapshot=chain_snapshot,
        scalping_signal=str(scalping_signal),
        entry_decision=str(entry_decision),
        confidence=float(confidence),
        aggregate_signal=str(agg_sig),
        aggregate_confidence=agg_conf_f,
        strike_label=strike_label,
        strike_num=strike_num_parsed,
        opt_type=opt_for_exec,
        entry=entry,
        target=target,
        stoploss=stoploss,
        chain_ltp_age_sec=chain_ltp_age_sec,
        skip_entry_reason=skip_entry,
        entry_context_reason=decision_reason,
    )
    ex_after = (state.get("_execution_trade") or {}).get(symbol) or {}
    ex_status = str(ex_after.get("status") or "").upper()
    open_sig = str(ex_after.get("signal") or "").upper()
    if (
        ex_status == "OPEN"
        and str(entry_decision).upper() in ("BUY_CE", "BUY_PE")
        and str(entry_decision).upper() != open_sig
    ):
        state.setdefault("execution_opportunity_signal", {})[symbol] = _build_opportunity_signal(
            symbol=symbol,
            signal=str(entry_decision).upper(),
            strike=strike_label,
            entry=entry,
            target=target,
            stoploss=stoploss,
            confidence=float(confidence),
            reason=f"new_signal_while_open|open={open_sig}|candidate={str(entry_decision).upper()}",
            index_price=float(price or 0.0),
        )
    elif ex_status != "OPEN":
        opp_bucket = state.get("execution_opportunity_signal")
        if isinstance(opp_bucket, dict) and symbol in opp_bucket:
            del opp_bucket[symbol]

    new_alert = (state.get("execution_final_signal") or {}).get(symbol)
    if new_alert and new_alert != prev_alert:
        execution_broker.dispatch_execution_event(symbol, new_alert)

    paper_trader.process_tick(symbol, final_fast, chain_snapshot, state)
    maybe_persist_execution_state(state)
    _update_realtime_latency(symbol, t_compute0, idx_age if isinstance(idx_age, (int, float)) else None)
    if os.getenv("AUDIT_LATENCY_JSONL", "").strip().lower() in ("1", "true", "yes", "on"):
        _append_audit_latency_row(symbol, t_compute0, float(price or 0.0))

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
        "oi_analysis": oi_snap,
        "ml": ml_signal,
        "scalping": {
            "trade": scalping_signal,
            "confidence": confidence,
            "entry_decision": entry_decision,
            "decision_reason": decision_reason,
            "stable_count": stable_count,
            "lock_remaining_sec": int(round(lock_remaining)),
            "strike": strike_label,
            "chain_ltp": chain_ltp,
            "chain_ltp_age_sec": chain_ltp_age_sec,
            "entry": entry,
            "target": target,
            "stoploss": stoploss,
            "option_expiry": option_expiry,
            "premium_target_mult": final_fast.get("premium_target_mult"),
            "premium_stop_mult": final_fast.get("premium_stop_mult"),
            "levels_basis": final_fast.get("levels_basis"),
            **aggregate_leg_fields,
        },
        "symbol": symbol,
        "price": float(price or 0.0),
        "engine_platform": ep,
        "execution_position": (state.get("_execution_trade") or {}).get(symbol),
        "execution_alert": (state.get("execution_final_signal") or {}).get(symbol),
        "execution_opportunity": (state.get("execution_opportunity_signal") or {}).get(symbol),
        "market_intelligence": market_intel,
        "strategy_config": strategy_cfg,
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
        "chain_ltp": chain_ltp,
        "chain_ltp_age_sec": chain_ltp_age_sec,
        "premium_target_mult": final_fast.get("premium_target_mult"),
        "premium_stop_mult": final_fast.get("premium_stop_mult"),
        "levels_basis": final_fast.get("levels_basis"),
        "option_expiry": option_expiry,
        "regime": None,
        "gamma_wall": None,
        "max_pain": None,
        "institutional_flow": None,
        "platform_aggregate": ep.get("aggregate"),
        "market_type": market_intel.get("market_type"),
        "strategy_mode": strategy_cfg.get("mode"),
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

    if scalping_signal != "NO_TRADE":
        logger.info("[FAST SIGNAL] %s price=%s confidence=%s", scalping_signal, price, confidence)


async def compute_for_symbol(symbol: str) -> None:
    """Run live tick in a worker thread to keep the event loop responsive under load."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _compute_for_symbol_impl, symbol)


def _live_tick_sleep_sec() -> float:
    try:
        s = float(os.getenv("LIVE_TICK_SLEEP_SEC", "0.25"))
    except ValueError:
        s = 0.25
    return max(0.05, min(5.0, s))


async def main_loop():
    sleep_sec = _live_tick_sleep_sec()
    logger.info("[LIVE] main loop tick interval=%.2fs (set LIVE_TICK_SLEEP_SEC to override)", sleep_sec)
    while True:
        for symbol in active_compute_symbols():
            try:
                await compute_for_symbol(symbol)
            except Exception as exc:
                logger.exception("Error in main loop for %s: %s", symbol, exc)
                if symbol not in state.get("signals", {}):
                    _publish_waiting_signal_state(symbol, f"tick_error:{type(exc).__name__}")
        await asyncio.sleep(sleep_sec)


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
    attach_error_file_handler()
    logger.info("Initializing schema...")
    init_schema()
    mark_startup(state)
    _refresh_market_status_state()
    mode_info = _apply_detected_mode("startup")
    logger.info("[MODE] startup mode=%s profile=%s", mode_info.get("mode"), mode_info.get("profile", {}).get("resolved_profile_file"))
    load_persisted_execution_state(state)
    paper_trader.initialize_on_api_startup(state)
    logger.info("[RUNTIME] API cold restart — stability guards active (warmup + tick gates + paper sync)")
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
        nifty_spot = _discovery_index_spot("NIFTY", "NIFTY_PROXY_SPOT", 23500.0)
        nifty_atm = float(os.getenv("NIFTY_ATM_RANGE", "600"))
        nifty_max = int(os.getenv("NIFTY_MAX_WS_TOKENS", "240"))
        # Keep SENSEX proxy close to live; far proxy picks illiquid strikes.
        sensex_spot = _discovery_index_spot("SENSEX", "SENSEX_PROXY_SPOT", 75000.0)
        sensex_atm = float(os.getenv("SENSEX_ATM_RANGE", "800"))
        sensex_max = int(os.getenv("SENSEX_MAX_WS_TOKENS", "140"))

        # 1 = front expiry only (typically current NIFTY weekly); raise to 2 for next serial too.
        nifty_exp = int(os.getenv("NIFTY_EXPIRY_COUNT", "1"))
        sensex_exp = int(os.getenv("SENSEX_EXPIRY_COUNT", "1"))
        n_toks, n_map = get_nifty_option_tokens(
            inst, nifty_spot, atm_range=nifty_atm, max_tokens=nifty_max, expiry_count=nifty_exp
        )
        sensex_on = "SENSEX" in active_compute_symbols()
        if sensex_on:
            s_toks, s_map = get_sensex_option_tokens(
                inst, sensex_spot, atm_range=sensex_atm, max_tokens=sensex_max, expiry_count=sensex_exp
            )
        else:
            s_toks, s_map = [], {}
            logger.info("[WS] SENSEX session off — subscribing NIFTY option tokens only (toggle via POST /session/sensex)")
        all_map = {**n_map, **s_map}
        state["_ws_subscribed_sensex_options"] = bool(s_map)
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

    if "SENSEX" not in active_compute_symbols():
        _clear_sensex_runtime_state()
    for sym in active_compute_symbols():
        if sym not in state["signals"]:
            _publish_waiting_signal_state(sym, "startup_awaiting_first_tick")

    if (os.getenv("RESET_PAPER_ON_STARTUP", "0").strip().lower() in ("1", "true", "yes", "on")):
        paper_trader.reset_paper_trades(state)
        logger.info("[PAPER] RESET_PAPER_ON_STARTUP=1 — paper history cleared at boot")

    asyncio.create_task(main_loop())
    asyncio.create_task(mode_watch_loop())
    slow_sec = max(0.5, float(os.getenv("SLOW_ENGINE_LOOP_SEC", "1.0")))
    asyncio.create_task(slow_engine_loop(state, active_compute_symbols, slow_sec))
    strat_sec = max(0.5, float(os.getenv("STRATEGY_LOOP_SEC", "1.0")))
    asyncio.create_task(strategy_signal_loop(state, active_compute_symbols, strat_sec))
    logger.info("API server ready. Docs: http://localhost:8000/docs")
    try:
        from app.services.telegram_execution_notify import send_startup_telegram_sync

        await loop.run_in_executor(None, send_startup_telegram_sync)
    except Exception as exc:
        logger.warning("Telegram startup notify skipped: %s", exc)


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
    sensex_on = "SENSEX" in active_compute_symbols()
    mstat = _refresh_market_status_state()
    return {
        "market": state["market"],
        "data_status": state.get("data_status") or {},
        "market_open": bool(mstat.get("is_open")),
        "market_status": mstat,
        "market_intelligence": state.get("market_intelligence") or {},
        "strategy_config": state.get("strategy_config") or {},
        "model_version": state.get("model_version"),
        "sensex_session_enabled": bool(sensex_on),
        "active_compute_symbols": active_compute_symbols(),
        "sensex_option_ws_subscribed": bool(state.get("_ws_subscribed_sensex_options")),
    }


@app.get("/session")
async def get_session():
    sensex_on = "SENSEX" in active_compute_symbols()
    return {
        "sensex_session_enabled": bool(sensex_on),
        "active_compute_symbols": active_compute_symbols(),
        "sensex_option_ws_subscribed": bool(state.get("_ws_subscribed_sensex_options")),
    }


@app.post("/session/sensex")
async def post_session_sensex(body: SensexSessionBody):
    """
    Turn SENSEX live compute on/off for this process. Default at boot: off unless SENSEX_SESSION_ENABLED=1.

    Enabling SENSEX after start without Sensex tokens in the WebSocket map requires an API restart
    to subscribe BFO options (see ``restart_required_for_sensex_option_chain`` in the response).
    """
    state["sensex_session_enabled"] = bool(body.enabled)
    restart_needed = bool(body.enabled) and not bool(state.get("_ws_subscribed_sensex_options"))
    if not body.enabled:
        _clear_sensex_runtime_state()
    else:
        _publish_waiting_signal_state("SENSEX", "sensex_enabled_awaiting_option_chain")
        if restart_needed:
            logger.warning(
                "[SESSION] SENSEX enabled but WS was started without SENSEX options — restart the API to stream SENSEX chain"
            )
    return {
        "ok": True,
        "sensex_session_enabled": ("SENSEX" in active_compute_symbols()),
        "active_compute_symbols": active_compute_symbols(),
        "restart_required_for_sensex_option_chain": restart_needed,
    }


@app.get("/signals")
async def get_signals():
    return {
        "signals": state["signals"],
        "data_status": state.get("data_status") or {},
        "market_status": state.get("market_status") or _refresh_market_status_state(),
        "market_intelligence": state.get("market_intelligence") or {},
        "strategy_config": state.get("strategy_config") or {},
        "model_version": state.get("model_version"),
        "execution_final_signal": state.get("execution_final_signal") or {},
        "execution_opportunity_signal": state.get("execution_opportunity_signal") or {},
        "execution_positions": state.get("_execution_trade") or {},
    }


@app.get("/engine-signals")
async def get_engine_signals():
    return {
        "engine_signals": state.get("engine_signals") or {},
        "active_compute_symbols": active_compute_symbols(),
    }


@app.get("/strategy-signals")
async def get_strategy_signals():
    return {
        "strategy_signals": state.get("strategy_signals") or {},
        "active_compute_symbols": active_compute_symbols(),
    }


@app.get("/latency")
async def get_latency():
    health = get_feed_health_snapshot() or {}
    mstat = state.get("market_status") or _refresh_market_status_state()
    return {
        "latency": state.get("latency") or {},
        "data_status": state.get("data_status") or {},
        "market_open": bool(mstat.get("is_open")),
        "market_status": mstat,
        "last_index_tick_age_sec": health.get("last_index_tick_age_sec"),
        "last_any_tick_age_sec": health.get("last_any_tick_age_sec"),
        "active_compute_symbols": active_compute_symbols(),
    }


@app.get("/system-mode")
async def get_system_mode():
    if not state.get("system_mode"):
        _refresh_market_status_state()
        _apply_detected_mode("lazy_init")
    return {
        "system_mode": state.get("system_mode") or {},
        "market_status": state.get("market_status") or {},
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
        if state.get("sensex_session_enabled"):
            sensex_snap = chains.get("SENSEX") or get_live_option_chain_snapshot("SENSEX")
            sx_chain = sensex_snap if sensex_snap else {}
            sx_df = option_chain_to_dataframe(sx_chain, symbol_prefix="SENSEX") if sx_chain else option_chain_to_dataframe({}, symbol_prefix="SENSEX")
            sx_analytics = compute_oi_analysis(sx_df) if not sx_df.empty else {}
            sensex_payload = {
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
            }
        else:
            sensex_payload = {
                "chain": {},
                "analytics": {},
                "hero_zero_strikes": [],
                "session_note": "SENSEX session off — enable via dashboard toggle or SENSEX_SESSION_ENABLED=1 and restart",
            }
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
            "SENSEX": sensex_payload,
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
            if sym == "SENSEX" and not state.get("sensex_session_enabled"):
                out[sym] = {
                    "index_price": None,
                    "coverage": _coverage({}),
                    "session_disabled": True,
                }
                continue
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


@app.get("/engines")
async def get_engines():
    """Per-engine outputs (scalping, hold, call_side, put_side, hero_zero) from last tick."""
    return {"engine_platform": state.get("engine_platform", {})}


@app.get("/hero-zero-expiry")
async def get_hero_zero_expiry():
    """Isolated NIFTY expiry-day spike experiment; does not affect scalping or execution."""
    return {"hero_zero_expiry": state.get("hero_zero_expiry", {})}


@app.get("/aggregate")
async def get_aggregate():
    """Weighted multi-engine decision and risk flags per symbol."""
    ep = state.get("engine_platform", {}) or {}
    agg = {}
    risk = {}
    for sym, pack in ep.items():
        if not isinstance(pack, dict):
            continue
        agg[sym] = pack.get("aggregate")
        risk[sym] = pack.get("risk")
    return {"aggregate": agg, "risk": risk}


@app.get("/dashboard")
async def get_engine_dashboard():
    """Combined view: market regime, agreement %, intent, S/R levels and engine outputs."""
    ep = state.get("engine_platform", {}) or {}
    market = state.get("market", {}) or {}
    out: Dict[str, Any] = {}
    for sym, pack in ep.items():
        if not isinstance(pack, dict):
            continue
        a = pack.get("aggregate") or {}
        meta = a.get("metadata") if isinstance(a, dict) else {}
        out[sym] = {
            "price": (market.get(sym) or {}).get("last_price"),
            "engines": pack.get("engines"),
            "aggregate": pack.get("aggregate"),
            "aggregate_raw": pack.get("aggregate_raw"),
            "risk": pack.get("risk"),
            "agreement_ratio": (meta or {}).get("agreement_ratio"),
            "signal_intent": (pack.get("aggregate") or {}).get("intent"),
            "market_regime": pack.get("regime"),
            "support_resistance": (pack.get("support_resistance") or {}).get("metadata"),
        }
    return {
        "dashboard": out,
        "fast_signals": state.get("fast_signals", {}),
    }


@app.get("/paper-trades")
async def get_paper_trades():
    """Paper-trading dashboard payload (active legs, stats, last trade, guide)."""
    paper_trader.bootstrap(state)
    return {"paper_trades": state.get("paper_trades", {"active": {}, "stats": {}, "last_trade": None, "guide": {}})}


@app.post("/paper-trades/reset")
async def post_reset_paper_trades():
    """Truncate logs/paper_trades.jsonl and zero stats, active positions, and last_trade."""
    out = paper_trader.reset_paper_trades(state)
    return {"ok": True, "paper_trades": out}


@app.get("/signal-history")
async def get_signal_history_endpoint(symbol: str = "NIFTY", limit: int = 50):
    """History of persisted fast signals (final_fast). Newest first."""
    history = get_signal_history(symbol=symbol, category="final_fast", limit=min(limit, 200))
    return {"symbol": symbol, "history": history}


@app.post("/signals/reset")
async def post_reset_signals(scope: str = "today_ist"):
    """
    Clear persisted fast-signal history (SQLite ``final_fast`` + ``logs/signal_events.jsonl``).

    Query ``scope``:
      - ``today_ist`` (default): remove rows from the current calendar day in Asia/Kolkata.
      - ``all``: delete every ``final_fast`` row and truncate the JSONL file.

    In-memory live signals are unchanged; this resets history used for /signal-history and audits.
    Also clears per-symbol DB/JSONL write cooldowns so recording resumes immediately.
    """
    s = str(scope or "today_ist").strip().lower()
    if s in ("all", "everything"):
        resolved: Literal["today_ist", "all"] = "all"
    else:
        resolved = "today_ist"
    out = reset_final_fast_signal_history(scope=resolved)
    state["_signal_record_cooldown"] = {}
    state["_fast_store_cooldown"] = {}
    return {"ok": True, **out}


def _ws_interval_sec(env_name: str, default: float) -> float:
    try:
        v = float(os.getenv(env_name, str(default)))
    except ValueError:
        v = default
    return max(0.05, min(2.0, v))


@app.websocket("/ws/signals")
async def websocket_signals(ws: WebSocket):
    await ws.accept()
    interval = _ws_interval_sec("WS_SIGNALS_PUSH_SEC", 0.25)
    try:
        while True:
            await ws.send_json({
                "fast": state.get("fast_signals", {}),
            })
            await asyncio.sleep(interval)
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
    interval = _ws_interval_sec("WS_FAST_PUSH_SEC", 0.2)
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
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        logger.info("Fast WebSocket client disconnected")

