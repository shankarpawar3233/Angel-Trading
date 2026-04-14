from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.services.websocket_service import get_index_ltp, get_option_chain_snapshot
from engines.breakout_engine import BreakoutEngine
from engines.market_state import MarketState
from engines.oi_engine import OiEngine
from engines.smc_engine import SmcEngine
from engines.trend_engine import TrendEngine
from utils.logger import get_logger

logger = get_logger(__name__)


def _quick_oi(chain: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    ce = 0.0
    pe = 0.0
    max_ce = (None, -1.0)
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
    }


def _fmt_engine_row(out: Dict[str, Any], ts: str) -> Dict[str, Any]:
    return {
        "signal": str(out.get("signal") or "NO_TRADE"),
        "confidence": float(out.get("confidence") or 0.0),
        "reason": str(out.get("reason") or ""),
        "timestamp": ts,
    }


def _agreement_summary(rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    directional = {k: v for k, v in rows.items() if str(v.get("signal") or "") in ("BUY_CE", "BUY_PE")}
    ce = [k for k, v in directional.items() if str(v.get("signal")) == "BUY_CE"]
    pe = [k for k, v in directional.items() if str(v.get("signal")) == "BUY_PE"]
    if len(ce) > len(pe):
        top = "BUY_CE"
    elif len(pe) > len(ce):
        top = "BUY_PE"
    else:
        top = "NO_TRADE"
    return {
        "dominant_signal": top,
        "ce_votes": ce,
        "pe_votes": pe,
        "directional_count": len(directional),
    }


def compute_slow_engine_symbol(global_state: Dict[str, Any], symbol: str) -> None:
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
    sym_hist = list((global_state.get("_price_history") or {}).get(su) or [])
    if sym_hist and float(sym_hist[-1]) != float(price):
        sym_hist.append(float(price))
        sym_hist = sym_hist[-128:]
    oi_snap = _quick_oi(chain)

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
        oi_snap=oi_snap,
        scalping_pipeline_result=None,
    )

    ts = datetime.now(timezone.utc).isoformat()
    trend = TrendEngine().process_tick(ms)
    smc = SmcEngine().process_tick(ms)
    breakout = BreakoutEngine().process_tick(ms)
    oi = OiEngine().process_tick(ms)
    scalping_live = ((global_state.get("signals", {}).get(su) or {}).get("scalping") or {})
    scalping = {
        "signal": str(scalping_live.get("trade") or scalping_live.get("signal") or "NO_TRADE"),
        "confidence": float(scalping_live.get("confidence") or 0.0),
        "reason": str(scalping_live.get("decision_reason") or "from_fast_execution_pipeline"),
        "timestamp": ts,
    }
    rows = {
        "scalping": scalping,
        "trend": _fmt_engine_row(trend, ts),
        "smc": _fmt_engine_row(smc, ts),
        "breakout": _fmt_engine_row(breakout, ts),
        "oi": _fmt_engine_row(oi, ts),
    }
    rows["agreement"] = _agreement_summary(rows)
    global_state.setdefault("engine_signals", {})[su] = rows


async def slow_engine_loop(global_state: Dict[str, Any], active_symbols_getter, interval_sec: float = 1.0) -> None:
    i = max(0.3, float(interval_sec))
    logger.info("[SLOW_ENGINES] started interval=%.2fs", i)
    loop = asyncio.get_running_loop()
    while True:
        syms: List[str] = list(active_symbols_getter() or [])
        tasks = [loop.run_in_executor(None, compute_slow_engine_symbol, global_state, s) for s in syms]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(i)
