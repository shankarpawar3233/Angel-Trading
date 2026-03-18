from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
from data.live_price import get_live_price
from fast_features import compute_fast_features
from hero_zero_fast import detect_hero_zero_fast
from scalping_fast import compute_fast_confidence, generate_fast_scalping_signal
from training.daily_trainer import train_models
from utils.logger import get_logger


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
    from data.angel_option_stream import get_live_option_chain_snapshot

    live_info = get_live_price(symbol)
    price = live_info.get("price") or 0.0
    price_source = live_info.get("source") if live_info.get("price") is not None else "cached"

    chain_snapshot = get_live_option_chain_snapshot()

    # Maintain short price history per symbol in process memory
    history = state.setdefault("_price_history", {})
    sym_hist = history.get(symbol) or []
    if price > 0:
        sym_hist.append(float(price))
        if len(sym_hist) > 128:
            sym_hist = sym_hist[-128:]
    history[symbol] = sym_hist

    features = compute_fast_features(chain_snapshot, float(price or 0.0), sym_hist)
    scalping_signal = generate_fast_scalping_signal(features)
    hero_zero = detect_hero_zero_fast(chain_snapshot, float(price or 0.0))
    confidence = compute_fast_confidence(features)

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
        # Basic ATM step
        step = strikes[1] - strikes[0] if len(strikes) > 1 else 50.0
        atm = min(strikes, key=lambda s: abs(s - ref_price))
        candidates: list[float] = [atm]
        if trade == "BUY_CE":
            up = atm + step
            if up in strikes:
                candidates.append(up)
        elif trade == "BUY_PE":
            down = atm - step
            if down in strikes:
                candidates.append(down)
        for s_val in candidates:
            key = str(int(s_val))
            row = chain.get(key) or {}
            leg = row.get(side)
            if not isinstance(leg, dict):
                continue
            ltp = leg.get("ltp")
            vol = float(leg.get("volume") or 0.0)
            oi = float(leg.get("oi") or 0.0)
            if ltp is None or vol <= 0 or oi <= 0:
                continue
            try:
                premium = float(ltp)
            except (TypeError, ValueError):
                continue
            return {"strike": s_val, "type": side, "premium": premium}
        return None

    if scalping_signal in ("BUY_CE", "BUY_PE"):
        sel = _select_fast_strike(chain_snapshot, float(price or 0.0), scalping_signal)
        if sel:
            s_val = sel["strike"]
            premium = sel["premium"]
            strike_label = f"{int(s_val)} {'CE' if scalping_signal == 'BUY_CE' else 'PE'}"
            entry = premium
            target = round(premium * 1.4, 2)
            stoploss = round(premium * 0.8, 2)

    final_fast = {
        "symbol": symbol,
        "price": float(price or 0.0),
        "signal": scalping_signal,
        "confidence": confidence,
        "hero_zero": hero_zero,
        "strike": strike_label,
        "entry": entry,
        "target": target,
        "stoploss": stoploss,
        "price_source": price_source,
    }

    ts = datetime.now(timezone.utc)
    state["market"][symbol] = {
        "symbol": symbol,
        "last_price": float(price or 0.0),
        "ts": ts.isoformat(),
        "price_source": price_source,
    }
    state["fast_signals"][symbol] = final_fast

    # Backwards-compatible minimal final_signal for existing UI (trade/hero_zero/confidence)
    state["final_signal"][symbol] = {
        "symbol": symbol,
        "price": float(price or 0.0),
        "trade": scalping_signal,
        "confidence": confidence,
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
    if scalping_signal != "NO_TRADE" or confidence >= 60:
        cooldown_state = state.setdefault("_fast_store_cooldown", {})
        last_ts = cooldown_state.get(symbol)
        allow_write = True
        if isinstance(last_ts, datetime):
            delta = (ts - last_ts).total_seconds()
            if delta < 10.0:
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

    if scalping_signal != "NO_TRADE":
        logger.info("[FAST SIGNAL] %s price=%s confidence=%s", scalping_signal, price, confidence)



async def main_loop():
    while True:
        for symbol in settings.indices:
            try:
                await compute_for_symbol(symbol)
            except Exception as exc:
                logger.exception("Error in main loop for %s: %s", symbol, exc)
        await asyncio.sleep(0.5)  # low-latency cadence


def _persist_option_ticks():
    """Persist live option chain snapshot to option_ticks table."""
    try:
        from data.angel_option_stream import get_live_option_chain_snapshot
        snap = get_live_option_chain_snapshot()
        if not snap:
            return
        ticks = [{"token": v.get("token"), "ltp": v.get("ltp"), "volume": v.get("volume"), "oi": v.get("oi"), "oi_change": v.get("oi_change")} for v in snap.values() if v.get("token")]
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

    # Option discovery + WebSocket stream for NIFTY
    try:
        from data.angel_option_discovery import get_subscription_tokens
        from data.angel_option_stream import start_option_stream
        live_info = get_live_price("NIFTY")
        index_price = live_info.get("price") if live_info else None
        # Tight ATM band and limited tokens for low-latency operation
        tokens = get_subscription_tokens(index_price=index_price, atm_band=5, max_tokens=50)
        if tokens:
            logger.info("[OPTIONS] Discovered %s NIFTY contracts for streaming", len(tokens))
            if start_option_stream(tokens):
                logger.info("[WS] Angel option stream connected")
                _scheduler.add_job(_persist_option_ticks, trigger="interval", seconds=60, id="persist_option_ticks", replace_existing=True)
        else:
            logger.warning("[OPTIONS] No NIFTY option tokens discovered")
    except Exception as exc:
        logger.warning("[WS] Option stream startup skipped: %s", exc)

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
        from data.angel_option_stream import get_live_option_chain_snapshot
        from features.option_chain_builder import build_option_chain, option_chain_to_dataframe
        from features.oi_analysis import compute_oi_analysis
        snap = get_live_option_chain_snapshot()
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
                "hero_zero_strikes": list(set(hero_strikes)),
            },
        }
    except Exception:
        return {"NIFTY": {"chain": {}, "analytics": {}, "hero_zero_strikes": []}}


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

