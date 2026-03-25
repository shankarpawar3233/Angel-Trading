from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from engines.aggregator import aggregate
from engines.market_state import MarketState
from engines.registry import default_engines
from engines.risk_engine import evaluate_risk
from engines.scalping_pipeline import run_scalping_pipeline


def run_engine_tick(
    global_state: Dict[str, Any],
    *,
    symbol: str,
    price: float,
    chain_snapshot: Dict[str, Dict[str, Dict[str, Any]]],
    sym_hist: List[float],
    oi_snap: Dict[str, Any],
    engines: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    One tick: run scalping pipeline once, build MarketState, run all engines, aggregate, risk.
    Mutates global_state decision + side_balance the same way as inline api path.
    """
    su = symbol.upper()
    decision_state = global_state.setdefault("_decision_state", {})
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
    side_balance_state = global_state.setdefault("_side_balance", {})
    now_sec = time.time()
    prev_raw = global_state.get("market", {}).get(symbol, {}).get("last_price")
    prev_market_price = float(prev_raw) if prev_raw is not None else None

    sp_result = run_scalping_pipeline(
        symbol=symbol,
        price=price,
        chain_snapshot=chain_snapshot,
        sym_hist=sym_hist,
        decision_ds=ds,
        side_balance_state=side_balance_state,
        now_sec=now_sec,
        prev_market_price=prev_market_price,
    )

    ms = MarketState(
        symbol=symbol,
        underlying=su,
        price=float(price),
        chain=chain_snapshot,
        price_history=list(sym_hist),
        now_epoch=now_sec,
        previous_index_price=prev_market_price,
        decision_ds=ds,
        side_balance_state=side_balance_state,
        oi_snap=dict(oi_snap or {}),
        scalping_pipeline_result=sp_result,
    )

    eng_list = engines if engines is not None else default_engines()
    engine_outputs: Dict[str, Dict[str, Any]] = {}
    for eng in eng_list:
        engine_outputs[eng.name] = eng.process_tick(ms)

    agg = aggregate(engine_outputs)
    risk_report, agg_after_risk = evaluate_risk(global_state, symbol, ms, agg)

    return {
        "scalping_pipeline": sp_result,
        "engines": engine_outputs,
        "aggregate": agg_after_risk,
        "aggregate_raw": agg,
        "risk": risk_report,
        "market_state_ts": now_sec,
    }
