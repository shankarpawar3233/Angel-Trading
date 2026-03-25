from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from engines.config import env_float, env_int
from engines.market_state import MarketState


def evaluate_risk(
    global_state: Dict[str, Any],
    symbol: str,
    market_state: MarketState,
    aggregate: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Returns (risk_report, possibly_modified_aggregate).
    Blocks on extreme short-window range, thin chain liquidity proxy, max signals/min, cooldown.
    """
    rs = global_state.setdefault("_risk_state", {})
    sym_r = rs.setdefault(symbol, {"last_emit_ts": 0.0, "window": []})

    hist = market_state.price_history
    price = float(market_state.price or 0.0)
    blocked = False
    reasons: List[str] = []

    if len(hist) >= 20 and price > 0:
        win = hist[-20:]
        rng = (max(win) - min(win)) / price
        max_r = env_float("RISK_MAX_RANGE_FRAC", 0.0045)
        if rng > max_r:
            blocked = True
            reasons.append(f"extreme_vol|range_frac={rng:.5f}|max={max_r}")

    strikes = len(market_state.chain or {})
    if strikes < env_int("RISK_MIN_STRIKES", 3):
        blocked = True
        reasons.append(f"low_liquidity|strikes={strikes}")

    legs = 0
    for sides in (market_state.chain or {}).values():
        if isinstance(sides, dict):
            legs += sum(1 for k in ("CE", "PE") if isinstance(sides.get(k), dict))
    if legs < env_int("RISK_MIN_OPTION_LEGS", 6):
        blocked = True
        reasons.append(f"thin_chain|legs={legs}")

    now = time.time()
    cooldown = env_float("RISK_COOLDOWN_SEC", 0.0)
    max_per_min = env_int("RISK_MAX_SIGNALS_PER_MIN", 200)

    w: List[float] = sym_r["window"]
    w[:] = [t for t in w if now - t < 60.0]
    if len(w) >= max_per_min:
        blocked = True
        reasons.append(f"rate_limit|n_60s={len(w)}")

    sig = str(aggregate.get("signal") or "NO_TRADE")
    if sig in ("BUY_CE", "BUY_PE") and not blocked:
        last = float(sym_r.get("last_emit_ts") or 0.0)
        if now - last < cooldown:
            blocked = True
            reasons.append(f"cooldown|{now - last:.2f}s_lt_{cooldown}")

    report = {
        "blocked": blocked,
        "reasons": reasons,
        "cooldown_sec": cooldown,
        "max_per_minute": max_per_min,
    }

    out_agg = dict(aggregate)
    if blocked and sig in ("BUY_CE", "BUY_PE"):
        out_agg = {
            **aggregate,
            "signal": "NO_TRADE",
            "confidence": min(float(aggregate.get("confidence") or 0.0), 40.0),
            "reason": f"risk_block:{';'.join(reasons) or 'unknown'}",
            "metadata": {**(aggregate.get("metadata") or {}), "risk_blocked": True},
        }
    elif sig in ("BUY_CE", "BUY_PE") and not blocked:
        sym_r["last_emit_ts"] = now
        w.append(now)

    return report, out_agg
