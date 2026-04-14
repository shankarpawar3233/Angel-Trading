from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import option_chain_service
from app.services.websocket_service import get_index_ltp, get_option_chain_snapshot
from engines.breakout_engine import BreakoutEngine
from engines.market_state import MarketState
from engines.smc_engine import SmcEngine
from engines.trend_engine import TrendEngine
from utils.logger import get_logger

logger = get_logger(__name__)


def _quick_oi(chain: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    ce = 0.0
    pe = 0.0
    for _, sides in (chain or {}).items():
        ce_leg = sides.get("CE") if isinstance(sides, dict) else None
        pe_leg = sides.get("PE") if isinstance(sides, dict) else None
        if isinstance(ce_leg, dict):
            ce += float(ce_leg.get("oi") or 0.0)
        if isinstance(pe_leg, dict):
            pe += float(pe_leg.get("oi") or 0.0)
    pcr = pe / (ce + 1e-9) if (ce > 0 or pe > 0) else 0.0
    return {"pcr": pcr, "ce_oi_total": ce, "pe_oi_total": pe}


def _leg_levels(chain: Dict[str, Dict[str, Dict[str, Any]]], price: float, signal: str) -> Dict[str, Optional[float]]:
    sig = str(signal or "").strip().upper()
    if sig not in ("BUY_CE", "BUY_PE"):
        return {"entry": None, "ltp": None, "sl": None, "target": None}
    sel = option_chain_service.select_strike_atm_window(
        chain,
        float(price or 0.0),
        sig,
        atm_steps_back=5,
        atm_steps_fwd=2,
    )
    if not sel:
        return {"entry": None, "ltp": None, "sl": None, "target": None}
    strike = float(sel["strike"])
    side = "CE" if sig == "BUY_CE" else "PE"
    row = option_chain_service.chain_row_for_strike(chain, strike)
    leg = row.get(side) if isinstance(row, dict) else None
    ltp = option_chain_service.option_leg_last_price(leg) if isinstance(leg, dict) else None
    if ltp is None:
        return {"entry": None, "ltp": None, "sl": None, "target": None}
    ltp_f = float(ltp)
    return {
        "entry": round(ltp_f, 2),
        "ltp": round(ltp_f, 2),
        "sl": round(ltp_f * 0.8, 2),
        "target": round(ltp_f * 1.4, 2),
    }


def _fmt_strategy_row(out: Dict[str, Any], chain: Dict[str, Dict[str, Dict[str, Any]]], index_price: float, ts: str) -> Dict[str, Any]:
    signal = str(out.get("signal") or "NO_TRADE").upper()
    levels = _leg_levels(chain, float(index_price or 0.0), signal)
    return {
        "signal": signal,
        "entry": levels.get("entry"),
        "ltp": levels.get("ltp"),
        "sl": levels.get("sl"),
        "target": levels.get("target"),
        "confidence": float(out.get("confidence") or 0.0),
        "reason": str(out.get("reason") or ""),
        "timestamp": ts,
    }


def compute_strategy_symbol(global_state: Dict[str, Any], symbol: str) -> None:
    su = str(symbol).upper()
    price_raw = get_index_ltp(su)
    if not price_raw:
        price_raw = (global_state.get("market", {}).get(su) or {}).get("last_price")
    try:
        price = float(price_raw or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return
    chain = get_option_chain_snapshot(su) or {}
    if not chain:
        return
    sym_hist = list((global_state.get("_price_history") or {}).get(su) or [])
    if sym_hist and float(sym_hist[-1]) != float(price):
        sym_hist.append(float(price))
        sym_hist = sym_hist[-128:]

    ms = MarketState(
        symbol=su,
        underlying=su,
        price=float(price),
        chain=chain,
        price_history=sym_hist,
        now_epoch=0.0,
        previous_index_price=(global_state.get("market", {}).get(su) or {}).get("last_price"),
        decision_ds={},
        side_balance_state={},
        oi_snap=_quick_oi(chain),
        scalping_pipeline_result=None,
    )

    ts = datetime.now(timezone.utc).isoformat()
    trend = TrendEngine().process_tick(ms)
    smc = SmcEngine().process_tick(ms)
    breakout = BreakoutEngine().process_tick(ms)
    ds = (global_state.get("data_status") or {}).get(su) or {}
    is_stale = not bool(ds.get("is_fresh", True))
    age_sec = float(ds.get("age_sec") or 0.0)
    rows = {
        "trend": _fmt_strategy_row(trend, chain, price, ts),
        "smc": _fmt_strategy_row(smc, chain, price, ts),
        "breakout": _fmt_strategy_row(breakout, chain, price, ts),
    }
    for k in ("trend", "smc", "breakout"):
        rows[k]["stale"] = is_stale
        rows[k]["data_age_sec"] = round(age_sec, 3)
    global_state.setdefault("strategy_signals", {})[su] = rows


async def strategy_signal_loop(global_state: Dict[str, Any], active_symbols_getter, interval_sec: float = 1.0) -> None:
    i = max(0.3, float(interval_sec))
    logger.info("[STRATEGY_SIGNALS] started interval=%.2fs", i)
    loop = asyncio.get_running_loop()
    while True:
        syms: List[str] = list(active_symbols_getter() or [])
        tasks = [loop.run_in_executor(None, compute_strategy_symbol, global_state, s) for s in syms]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(i)
