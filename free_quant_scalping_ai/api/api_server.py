from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Dict

import pandas as pd

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
    store_regime,
    store_signal,
)
from data.historical_fetcher import bootstrap_historical_data
from data.live_price import get_live_price
from features.gamma_exposure import compute_gamma_exposure
from features.liquidity_map import build_liquidity_map
from features.max_pain import compute_max_pain
from features.market_regime import detect_market_regime
from features.oi_analysis import compute_oi_analysis
from features.feature_engineering import engineer_all_features
from strategies.ce_pe_signal_engine import build_final_signal, merge_signals
from strategies.stop_hunt_detector import detect_stop_hunt

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
from strategies.expiry_direction import compute_expiry_direction
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
    "final_signal": {},
    "market_regime": {},
    "liquidity_map": {},
    "stop_hunts": {},
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

    # Option chain: Angel WebSocket only (no NSE). Fallback to DB only if stream not yet populated.
    try:
        from data.angel_option_stream import get_live_option_chain_snapshot
        from features.option_chain_builder import option_chain_to_dataframe
        snap = get_live_option_chain_snapshot()
        option_chain = option_chain_to_dataframe(snap, symbol_prefix=symbol)
        if option_chain.empty:
            option_chain = latest_option_chain(symbol)
    except Exception:
        option_chain = latest_option_chain(symbol)
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
    gamma_exposure = compute_gamma_exposure(option_chain) if not option_chain.empty else {}
    max_pain_result = compute_max_pain(option_chain) if not option_chain.empty else {}
    expiry_bias = compute_expiry_direction(
        spot_price=index_price or 0.0,
        max_pain=max_pain_result.get("max_pain"),
        pcr=oi_analysis.get("pcr"),
        gamma_wall_call=gamma_exposure.get("gamma_wall_call"),
        gamma_wall_put=gamma_exposure.get("gamma_wall_put"),
        option_chain=option_chain if not option_chain.empty else None,
    )

    # Liquidity map (OI + volume + gamma)
    liquidity_map = {}
    if not option_chain.empty:
        liquidity_map = build_liquidity_map(
            option_chain,
            gamma_levels=gamma_exposure.get("gamma_levels"),
        )

    # Market regime (EMA, VWAP, ATR, RSI, volume)
    regime_result = detect_market_regime(candles)
    try:
        from models.regime_hmm import predict_regime_hmm
        hmm_regime = predict_regime_hmm(candles)
        regime_result["hmm_probabilities"] = hmm_regime.get("probabilities")
        regime_result["confidence"] = max(regime_result.get("confidence", 0), hmm_regime.get("confidence", 0))
    except Exception:
        pass

    # Stop-hunt detection
    stop_hunt_result = detect_stop_hunt(candles, option_chain, liquidity_map)

    fused = merge_signals(
        ml_decision, rl_decision, scalping, hero_zero, inst, gamma, expiry, sweep, model_version=model_version,
        oi_analysis=oi_analysis,
        gamma_exposure=gamma_exposure,
        max_pain=max_pain_result,
        expiry_bias=expiry_bias,
        liquidity_map=liquidity_map,
        regime=regime_result,
        stop_hunt=stop_hunt_result,
    )

    final_signal = build_final_signal(symbol, index_price or 0.0, fused)

    ts = datetime.now(timezone.utc)
    state["market"][symbol] = {
        "symbol": symbol,
        "last_price": index_price,
        "ts": ts.isoformat(),
        "price_source": price_source,
    }
    state["signals"][symbol] = fused
    state["final_signal"][symbol] = final_signal
    state["market_regime"][symbol] = regime_result
    state["liquidity_map"][symbol] = liquidity_map
    state["stop_hunts"][symbol] = stop_hunt_result

    # Store regime history
    store_regime(
        symbol=symbol,
        ts=ts,
        regime=regime_result.get("regime", "RANGE"),
        confidence=regime_result.get("confidence"),
        payload_json=json.dumps(regime_result, default=str),
    )

    # Store combined JSON signal
    store_signal(
        symbol=symbol,
        ts=ts,
        category="combined",
        payload_json=json.dumps(fused, default=str),
    )

    # Section 18 logging
    logger.info("[REGIME] Market regime detected: %s (confidence %s)", regime_result.get("regime"), regime_result.get("confidence"))
    if hero_zero.get("candidates"):
        logger.info("[HERO] Hero-zero candidate: %s", hero_zero["candidates"][0].get("strike"))
    if gamma_exposure.get("gamma_wall_call") or gamma_exposure.get("gamma_wall_put"):
        logger.info("[GAMMA] Gamma wall detected call=%s put=%s", gamma_exposure.get("gamma_wall_call"), gamma_exposure.get("gamma_wall_put"))
    if inst.get("flow_type") and inst.get("flow_type") != "NEUTRAL":
        logger.info("[FLOW] Institutional buildup: %s", inst.get("flow_type"))
    if stop_hunt_result.get("detected"):
        logger.info("[STOPHUNT] Liquidity grab detected: %s", stop_hunt_result.get("stop_hunt_zone"))

    scalping_sig = scalping.get("signal")
    logger.info(
        "LIVE SIGNAL %s price=%s trade=%s confidence=%s scalping=%s hero_zero=%s inst=%s gamma=%s expiry=%s",
        symbol,
        index_price,
        final_signal.get("trade"),
        final_signal.get("confidence"),
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
        chain = build_option_chain(snap)
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


@app.websocket("/ws/signals")
async def websocket_signals(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json({
                "market": state["market"],
                "signals": state["signals"],
                "final_signal": state.get("final_signal", {}),
                "market_regime": state.get("market_regime", {}),
                "liquidity_map": state.get("liquidity_map", {}),
                "stop_hunts": state.get("stop_hunts", {}),
            })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")

