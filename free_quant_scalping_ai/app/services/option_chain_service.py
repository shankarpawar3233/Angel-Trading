"""
ATM-relative strike selection for fast scalping (premium + liquidity score).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def chain_row_for_strike(chain: Dict[str, Any], s_val: float) -> Dict[str, Any]:
    """Resolve option row dict for a strike; keys vary by feed (int string, float string, etc.)."""
    if not isinstance(chain, dict) or not chain:
        return {}
    for key in (str(int(s_val)), str(s_val), str(float(s_val))):
        row = chain.get(key)
        if isinstance(row, dict) and row:
            return row
    try:
        target = float(s_val)
    except (TypeError, ValueError):
        return {}
    for k, row in chain.items():
        if not isinstance(row, dict):
            continue
        try:
            if abs(float(k) - target) < 1e-6:
                return row
        except (TypeError, ValueError):
            continue
    return {}


def option_leg_last_price(leg: Any) -> Optional[float]:
    """Best-effort option LTP from WS / REST snapshots (keys differ by source)."""
    if not isinstance(leg, dict):
        return None
    for key in ("ltp", "last_price", "lastPrice", "close"):
        raw = leg.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _leg_float_field(leg: Dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        raw = leg.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def option_leg_spread_pct(leg: Any) -> Optional[float]:
    """Bid–ask as %% of mid when both sides exist; otherwise None (caller should not filter)."""
    if not isinstance(leg, dict):
        return None
    bid = _leg_float_field(leg, ("bid", "bidprice", "bp", "buyPrice", "buy_price", "best_bid"))
    ask = _leg_float_field(leg, ("ask", "askprice", "sp", "sellPrice", "sell_price", "best_ask", "offer"))
    if bid is None or ask is None or ask <= bid:
        return None
    mid = 0.5 * (bid + ask)
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _strike_premium_floor() -> float:
    soft = _env_float("STRIKE_PREMIUM_MIN", 30.0)
    hard = _env_float("EXEC_HARD_MIN_PREMIUM", 10.0)
    return max(soft, hard)


def _max_spread_pct() -> float:
    """0 disables spread-based strike rejection."""
    try:
        return float(os.getenv("EXEC_MAX_SPREAD_PCT", "0"))
    except ValueError:
        return 0.0


def _leg_spread_rejects(leg: Any) -> bool:
    cap = _max_spread_pct()
    if cap <= 0:
        return False
    sp = option_leg_spread_pct(leg)
    if sp is None:
        return False
    return sp > cap


def select_strike_for_scalp(
    chain: Dict[str, Dict[str, Dict[str, Any]]],
    ref_price: float,
    trade: str,
) -> Optional[Dict[str, Any]]:
    """
    Pick best strike near ATM for CE/PE using volume+OI score.
    Premium band is env-tunable (defaults widened vs old 80–250).
    """
    if not chain or ref_price <= 0:
        return None
    side = "CE" if trade == "BUY_CE" else "PE" if trade == "BUY_PE" else None
    if side is None:
        return None
    prem_min = _strike_premium_floor()
    prem_max = _env_float("STRIKE_PREMIUM_MAX", 450.0)
    max_steps = int(_env_float("STRIKE_ATM_STEPS", 8.0))

    try:
        strikes = sorted(float(s) for s in chain.keys())
    except Exception:
        return None
    if not strikes:
        return None
    strike_step = abs(strikes[1] - strikes[0]) if len(strikes) > 1 else 50.0
    if strike_step <= 0:
        strike_step = 50.0
    # Use nearest listed strike (not rounded synthetic ATM), then expand by steps.
    atm = min(strikes, key=lambda s: abs(s - ref_price))
    atm_idx = strikes.index(atm)
    lo = max(0, atm_idx - max_steps)
    hi = min(len(strikes), atm_idx + max_steps + 1)
    candidates = strikes[lo:hi]

    # 1) minimize (distance from spot + small side penalty)
    # 2) maximize liquidity score as tie-breaker
    # This avoids selecting far strikes with huge OI/volume.
    best: Optional[Dict[str, Any]] = None
    best_rank: Optional[tuple[float, float]] = None

    for s_val in candidates:
        row = chain_row_for_strike(chain, float(s_val))
        leg = row.get(side) if isinstance(row, dict) else None
        if not isinstance(leg, dict):
            continue
        premium = option_leg_last_price(leg)
        if premium is None:
            continue
        if premium < prem_min or premium > prem_max:
            continue
        if _leg_spread_rejects(leg):
            continue
        vol = float(leg.get("volume") or 0.0)
        oi = float(leg.get("oi") or 0.0)
        dist_steps = abs(s_val - ref_price) / strike_step
        side_penalty = 0.0
        # Prefer CE at/above spot (OTM calls); PE at/below spot (OTM puts).
        if side == "CE" and s_val < ref_price:
            side_penalty = 0.35
        elif side == "PE" and s_val > ref_price:
            side_penalty = 0.35
        distance_rank = dist_steps + side_penalty
        liquidity_rank = -(vol * 0.001 + oi * 0.000001)
        rank = (distance_rank, liquidity_rank)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = {"strike": s_val, "type": side, "premium": premium}

    if best is not None:
        return best

    atm_row = chain_row_for_strike(chain, float(atm))
    atm_leg = atm_row.get(side) if isinstance(atm_row, dict) else None
    if isinstance(atm_leg, dict):
        atm_premium = option_leg_last_price(atm_leg)
        if atm_premium is not None:
            return {"strike": atm, "type": side, "premium": atm_premium}
    return None


def select_strike_atm_window(
    chain: Dict[str, Dict[str, Dict[str, Any]]],
    ref_price: float,
    trade: str,
    *,
    atm_steps_back: int = 5,
    atm_steps_fwd: int = 2,
    ltp_prev_map: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Restrict scan to ATM - atm_steps_back .. ATM + atm_steps_fwd (inclusive of listed strikes only).
    Score: liquidity + optional LTP delta vs previous tick (key ``f\"{strike}_{side}\"``).
    """
    if not chain or ref_price <= 0:
        return None
    side = "CE" if trade == "BUY_CE" else "PE" if trade == "BUY_PE" else None
    if side is None:
        return None
    prem_min = _strike_premium_floor()
    prem_max = _env_float("STRIKE_PREMIUM_MAX", 450.0)
    try:
        strikes = sorted(float(s) for s in chain.keys())
    except Exception:
        return None
    if not strikes:
        return None
    strike_step = abs(strikes[1] - strikes[0]) if len(strikes) > 1 else 50.0
    if strike_step <= 0:
        strike_step = 50.0
    atm = min(strikes, key=lambda s: abs(s - ref_price))
    atm_idx = strikes.index(atm)
    lo = max(0, atm_idx - int(atm_steps_back))
    hi = min(len(strikes), atm_idx + int(atm_steps_fwd) + 1)
    candidates = strikes[lo:hi]

    # Prefer strikes closest to spot first (same idea as select_strike_for_scalp). A pure
    # liquidity+move score can pick a strike 100+ points away on expiry when OI piles on
    # a stale strike — then LTP/entry no longer match the broker's ATM.
    best: Optional[Dict[str, Any]] = None
    best_rank: Optional[tuple[float, float]] = None
    prev = ltp_prev_map or {}

    for s_val in candidates:
        row = chain_row_for_strike(chain, float(s_val))
        leg = row.get(side) if isinstance(row, dict) else None
        if not isinstance(leg, dict):
            continue
        premium = option_leg_last_price(leg)
        if premium is None or premium < prem_min or premium > prem_max:
            continue
        if _leg_spread_rejects(leg):
            continue
        vol = float(leg.get("volume") or 0.0)
        oi = float(leg.get("oi") or 0.0)
        k = f"{int(s_val)}_{side}"
        prev_ltp = prev.get(k)
        move = 0.0
        if prev_ltp is not None:
            try:
                move = abs(float(premium) - float(prev_ltp))
            except (TypeError, ValueError):
                move = 0.0
        dist_steps = abs(s_val - ref_price) / strike_step
        side_penalty = 0.0
        if side == "CE" and s_val < ref_price:
            side_penalty = 0.35
        elif side == "PE" and s_val > ref_price:
            side_penalty = 0.35
        distance_rank = dist_steps + side_penalty
        liquidity = vol * 0.001 + oi * 1e-6
        activity = liquidity + move * 2.0
        # Lexicographic: nearer to ref wins; tie-break higher activity (liquidity + move).
        rank = (distance_rank, -activity)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = {"strike": s_val, "type": side, "premium": premium}

    if best is not None:
        return best
    return select_strike_for_scalp(chain, ref_price, trade)
