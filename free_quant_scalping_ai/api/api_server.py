from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from data.data_storage import (
    init_schema,
    insert_option_ticks,
    latest_option_chain,
    load_candles,
    store_signal,
)
from data.historical_fetcher import bootstrap_historical_data
from data.live_price import get_live_price
from data.nse_fetcher import fetch_and_store_option_chain
from features.oi_analysis import compute_oi_analysis
from features.feature_engineering import engineer_all_features
from strategies.ce_pe_signal_engine import merge_signals

try:
    from models.predictor import CombinedPredictor
    _HAS_PREDICTOR = True
except Exception:
    CombinedPredictor = None
    _HAS_PREDICTOR = False

try:
    from reinforcement.rl_agent import RLTradingAgent
    _HAS_RL = True
except Exception:
    RLTradingAgent = None
    _HAS_RL = False
from strategies.expiry_prediction_engine import expiry_prediction
from strategies.gamma_exposure_engine import analyze_gamma
from strategies.hero_zero_detector import detect_hero_zero
from strategies.hero_zero_live import detect_hero_zero_live
from strategies.institutional_flow_engine import detect_institutional_flow_from_chain
from strategies.liquidity_sweep_detector import detect_liquidity_sweep
from strategies.scalping_engine import generate_scalping_signal
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
    "model_version": None,
}

_scheduler: BackgroundScheduler | None = None


async def compute_for_symbol(symbol: str):
    # Try intraday first; if unavailable fall back to higher timeframes
    candles = load_candles(symbol, "1m", limit=800)
    if candles.empty:
        candles = load_candles(symbol, "5m", limit=800)
    if candles.empty:
        candles = load_candles(symbol, "15m", limit=800)
    if candles.empty:
        candles = load_candles(symbol, "1d", limit=800)

    feats = engineer_all_features(candles)
    if feats.empty:
        return

    feature_names = [
        c
        for c in feats.columns
        if c
        not in {"ts", "open", "high", "low", "close", "volume", "support", "resistance"}
    ]

    model_version: str | None = None
    if _HAS_PREDICTOR and CombinedPredictor is not None:
        predictor = CombinedPredictor(feature_names)
        ml_decision = predictor.predict(feats)
        model_version = predictor.model_version
    else:
        ml_decision = {"label": "NO_TRADE", "probs": {"BUY_CE": 0.33, "BUY_PE": 0.33, "NO_TRADE": 0.34}}

    import numpy as np
    X = feats[feature_names].values.astype("float32")
    X = np.nan_to_num(X, nan=0.0)
    prices = feats["close"].values.astype("float32")
    if not _HAS_RL or RLTradingAgent is None or len(X) < 100:
        rl_decision = {"action": "HOLD"}
    else:
        agent = RLTradingAgent(features=X, prices=prices)
        agent.train(timesteps=2_000)
        rl_decision = agent.predict_action(X[-1])

    # Option chain: prefer live stream for NIFTY, else NSE stored
    try:
        from data.angel_option_stream import get_live_option_chain_as_dataframe
        live_chain = get_live_option_chain_as_dataframe(symbol_prefix=symbol)
        if not live_chain.empty:
            option_chain = live_chain
        else:
            option_chain = latest_option_chain(symbol)
    except Exception:
        option_chain = latest_option_chain(symbol)
    # Ensure change_oi column for engines that expect it
    if not option_chain.empty and "change_oi" not in option_chain.columns and "oi_change" in option_chain.columns:
        option_chain = option_chain.copy()
        option_chain["change_oi"] = option_chain["oi_change"]

    live_info = get_live_price(symbol)
    live_price = live_info.get("price")
    price_source = live_info.get("source") if live_price is not None else "cached"
    index_price = live_price if live_price is not None else (float(candles["close"].iloc[-1]) if not candles.empty else None)
    scalping = generate_scalping_signal(
        symbol, candles, option_chain, ml_decision, rl_decision, live_price_override=live_price
    )
    # Hero-Zero: use live detector when we have option chain with ltp/volume
    if not option_chain.empty and "ltp" in option_chain.columns and "volume" in option_chain.columns:
        hero_zero = detect_hero_zero_live(option_chain, index_price or 0.0)
    else:
        hero_zero = detect_hero_zero(symbol, option_chain, index_price or 0.0)
    inst = detect_institutional_flow_from_chain(option_chain)
    sweep = detect_liquidity_sweep(candles)
    gamma = analyze_gamma(symbol, option_chain)
    expiry = expiry_prediction(symbol, option_chain)
    oi_analysis = compute_oi_analysis(option_chain) if not option_chain.empty else {}

    fused = merge_signals(
        ml_decision, rl_decision, scalping, hero_zero, inst, gamma, expiry, sweep, model_version=model_version,
        oi_analysis=oi_analysis,
    )

    ts = datetime.now(timezone.utc)
    state["market"][symbol] = {
        "symbol": symbol,
        "last_price": index_price,
        "ts": ts.isoformat(),
        "price_source": price_source,
    }
    state["signals"][symbol] = fused

    # Store combined JSON signal
    store_signal(
        symbol=symbol,
        ts=ts,
        category="combined",
        payload_json=json.dumps(fused, default=str),
    )

    # Console-style log
    scalping_sig = scalping.get("signal")
    logger.info(
        "LIVE SIGNAL %s price=%s scalping=%s hero_zero=%s inst=%s gamma=%s expiry=%s",
        symbol,
        index_price,
        scalping_sig,
        hero_zero.get("candidates"),
        inst.get("summary"),
        gamma,
        expiry.get("prediction"),
    )


async def main_loop():
    while True:
        logger.info("Main loop tick...")
        for symbol in settings.indices:
            try:
                logger.info("Fetching option chain for %s", symbol)
                fetch_and_store_option_chain(symbol)
                await compute_for_symbol(symbol)
            except Exception as exc:
                logger.exception("Error in main loop for %s: %s", symbol, exc)
        await asyncio.sleep(60)  # run roughly every minute


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
        tokens = get_subscription_tokens(index_price=index_price, atm_band=20, max_tokens=200)
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
    """Live option chain snapshot (from WebSocket if running)."""
    try:
        from data.angel_option_stream import get_live_option_chain_snapshot
        snap = get_live_option_chain_snapshot()
        return {"NIFTY": snap}
    except Exception:
        return {"NIFTY": {}}


@app.get("/oi")
async def get_oi():
    """OI analysis (PCR, max OI strikes, OI spikes) per symbol."""
    out = {}
    for symbol, sigs in state["signals"].items():
        oi = sigs.get("oi_analysis")
        if oi:
            out[symbol] = oi
    return out


@app.websocket("/ws/signals")
async def websocket_signals(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json({"market": state["market"], "signals": state["signals"]})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")

