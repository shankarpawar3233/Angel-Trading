"""
Isolated Hero Zero–style expiry scanner for NIFTY.

Read-only consumer of ``chain_snapshot`` and ``state["_price_history"]``.
Does not plug into scalping, aggregator, or execution.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.services import option_chain_service
from engines.hero_zero_engine import hero_zero_gate
from utils.logger import get_logger

logger = get_logger(__name__)

INTERNAL_KEY = "_hero_zero_expiry_internal"
Confidence = Literal["HIGH", "MEDIUM", "LOW"]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _iso_ts(now_ts: float) -> str:
    return datetime.fromtimestamp(float(now_ts), tz=timezone.utc).isoformat()


def default_no_trade_result(now_ts: float, reason: str) -> Dict[str, Any]:
    return {
        "signal": "NO_TRADE",
        "strike": None,
        "entry": None,
        "target": None,
        "sl": None,
        "strength": 0.0,
        "confidence": "LOW",
        "reason": reason,
        "time": _iso_ts(now_ts),
    }


def _internal(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.setdefault(INTERNAL_KEY, {})


def _leg_metrics(leg: Any) -> Tuple[float, float, float, float]:
    if not isinstance(leg, dict):
        return 0.0, 0.0, 0.0, 0.0
    ltp = float(option_chain_service.option_leg_last_price(leg) or 0.0)
    vol = float(leg.get("volume") or 0.0)
    oi = float(leg.get("oi") or 0.0)
    coi = leg.get("change_oi")
    if coi is None:
        coi = leg.get("oi_change")
    try:
        coi_f = float(coi) if coi is not None else 0.0
    except (TypeError, ValueError):
        coi_f = 0.0
    return ltp, vol, oi, coi_f


def _index_momentum(state: Dict[str, Any], symbol: str) -> float:
    hist = (state.get("_price_history") or {}).get(symbol) or []
    if len(hist) >= 4:
        return float(hist[-1]) - float(hist[-4])
    if len(hist) >= 2:
        return float(hist[-1]) - float(hist[-2])
    return 0.0


def _atm_window_strikes(chain: Dict[str, Any], ref_price: float, steps: int) -> List[float]:
    if not chain or ref_price <= 0:
        return []
    try:
        strikes = sorted(float(k) for k in chain.keys())
    except (TypeError, ValueError):
        return []
    if not strikes:
        return []
    atm = min(strikes, key=lambda s: abs(s - ref_price))
    try:
        idx = strikes.index(atm)
    except ValueError:
        idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - ref_price))
    lo = max(0, idx - steps)
    hi = min(len(strikes), idx + steps + 1)
    return strikes[lo:hi]


def run_hero_zero_expiry_engine(
    state: Dict[str, Any],
    symbol: str,
    price: float,
    chain_snapshot: Dict[str, Dict[str, Dict[str, Any]]],
    now_ts: float,
) -> Dict[str, Any]:
    """
    Produce an experimental expiry-day spike signal for NIFTY only.
    Mutates only ``state[INTERNAL_KEY]`` (prior leg snapshot + throttle metadata).
    """
    su = str(symbol or "").strip().upper()
    iso = _iso_ts(now_ts)

    if su != "NIFTY":
        return default_no_trade_result(now_ts, "symbol_not_nifty")

    active, gate_reason = hero_zero_gate(su)
    if not active:
        return default_no_trade_result(now_ts, f"not_expiry_day:{gate_reason}")

    prem_min = _env_float("HZ_EXP_PREM_MIN", 20.0)
    prem_max = _env_float("HZ_EXP_PREM_MAX", 300.0)
    atm_steps = _env_int("HZ_EXP_ATM_STEPS", 5)
    vol_ratio = _env_float("HZ_EXP_VOL_SPIKE_RATIO", 1.22)
    oi_ratio = _env_float("HZ_EXP_OI_SPIKE_RATIO", 1.06)
    prem_ratio = _env_float("HZ_EXP_PREM_JUMP_RATIO", 1.08)
    min_flags = max(1, _env_int("HZ_EXP_MIN_SCORE_FLAGS", 2))
    index_eps = _env_float("HZ_EXP_INDEX_EPS", 2.5)
    min_vol = _env_float("HZ_EXP_MIN_VOLUME", 120.0)
    coi_abs_min = _env_float("HZ_EXP_MIN_ABS_CHANGE_OI", 500.0)
    dup_sec = _env_float("HZ_EXP_DUP_STRIKE_SEC", 45.0)
    thr_delta = _env_float("HZ_EXP_THROTTLE_STRENGTH_DELTA", 0.08)
    tgt_mult = _env_float("HZ_EXP_TARGET_MULT", 1.35)
    sl_mult = _env_float("HZ_EXP_SL_MULT", 0.72)

    if not chain_snapshot or float(price or 0) <= 0:
        return default_no_trade_result(now_ts, "empty_chain_or_price")

    inn = _internal(state)
    sym_bucket = inn.setdefault(su, {})
    prev_flat: Dict[str, Dict[str, float]] = sym_bucket.get("leg_snap") or {}

    window = _atm_window_strikes(chain_snapshot, float(price), atm_steps)
    if not window:
        return default_no_trade_result(now_ts, "no_strikes_in_window")

    # Build fresh snapshot for next tick (current metrics only).
    new_flat: Dict[str, Dict[str, float]] = {}
    for s in window:
        row = option_chain_service.chain_row_for_strike(chain_snapshot, float(s))
        for ot in ("CE", "PE"):
            leg = row.get(ot) if isinstance(row, dict) else None
            k = f"{int(s)}_{ot}"
            ltp, vol, oi, coi = _leg_metrics(leg)
            new_flat[k] = {"ltp": ltp, "vol": vol, "oi": oi, "coi": coi}

    best: Optional[Tuple[str, int, float, float, str, int]] = None
    # tuple: side CE|PE, strike_int, ltp, strength, reason_flags, flag_count

    for s in window:
        row = option_chain_service.chain_row_for_strike(chain_snapshot, float(s))
        for ot in ("CE", "PE"):
            leg = row.get(ot) if isinstance(row, dict) else None
            ltp, vol, oi, coi = _leg_metrics(leg)
            if ltp < prem_min or ltp > prem_max:
                continue
            if vol < min_vol:
                continue
            k = f"{int(s)}_{ot}"
            p = prev_flat.get(k) or {}
            pv = float(p.get("vol") or 0.0)
            po = float(p.get("oi") or 0.0)
            pltp = float(p.get("ltp") or 0.0)
            pcoi = float(p.get("coi") or 0.0)

            flags: List[str] = []
            if pv > 0 and vol >= pv * vol_ratio:
                flags.append("vol_spike")
            elif pv <= 0 and vol >= min_vol * 1.5:
                flags.append("vol_spike_cold")

            if po > 0 and oi >= po * oi_ratio:
                flags.append("oi_spike")
            if abs(coi - pcoi) >= coi_abs_min and abs(coi) >= coi_abs_min * 0.5:
                flags.append("oi_change")

            if pltp > 0 and ltp >= pltp * prem_ratio:
                flags.append("prem_jump")

            fc = len(flags)
            if fc < min_flags:
                continue

            strength = min(1.0, 0.35 * fc + (0.12 if "prem_jump" in flags else 0) + (0.08 if "oi_change" in flags else 0))
            reason = "+".join(flags)
            cand = (ot, int(s), ltp, strength, reason, fc)
            if best is None or cand[3] > best[3] or (cand[3] == best[3] and cand[5] > best[5]):
                best = cand

    sym_bucket["leg_snap"] = new_flat

    mom = _index_momentum(state, symbol)
    if best is None:
        return default_no_trade_result(now_ts, "no_spike_candidate_atm_window")

    side, strike_i, entry_f, strength, flag_reason, _fc = best
    want_ce = side == "CE"

    if want_ce and mom < index_eps:
        return default_no_trade_result(
            now_ts,
            f"direction_mismatch:want_ce_mom{mom:.1f}<{index_eps}",
        )
    if not want_ce and mom > -index_eps:
        return default_no_trade_result(
            now_ts,
            f"direction_mismatch:want_pe_mom{mom:.1f}>-{index_eps}",
        )

    sig = "BUY_CE" if want_ce else "BUY_PE"
    tgt = round(entry_f * tgt_mult, 2)
    sl = round(entry_f * sl_mult, 2)

    if strength >= 0.75 and abs(mom) >= index_eps * 1.4:
        conf: Confidence = "HIGH"
    elif strength >= 0.48:
        conf = "MEDIUM"
    else:
        conf = "LOW"

    last_emit = sym_bucket.get("last_emit") or {}
    dup_record = sym_bucket.get("dup_guard") or {}

    # Duplicate strike / side within short window (unless much stronger).
    dr_ts = float(dup_record.get("ts") or 0.0)
    if now_ts - dr_ts < dup_sec:
        if int(dup_record.get("strike") or -1) == strike_i and str(dup_record.get("side") or "") == side:
            if strength <= float(dup_record.get("strength") or 0.0) + thr_delta:
                prev_out = sym_bucket.get("last_out")
                if isinstance(prev_out, dict):
                    return prev_out
                return default_no_trade_result(now_ts, "duplicate_strike_window")

    # Throttle: same signal+strike unless strength improves materially.
    if (
        last_emit.get("signal") == sig
        and int(last_emit.get("strike") or -1) == strike_i
        and strength <= float(last_emit.get("strength") or 0.0) + thr_delta
    ):
        prev_out = sym_bucket.get("last_out")
        if isinstance(prev_out, dict):
            return prev_out

    out = {
        "signal": sig,
        "strike": strike_i,
        "entry": round(entry_f, 4),
        "target": tgt,
        "sl": sl,
        "strength": round(strength, 4),
        "confidence": conf,
        "reason": f"{flag_reason};mom={mom:.2f};gate={gate_reason}",
        "time": iso,
    }

    sym_bucket["last_emit"] = {"signal": sig, "strike": strike_i, "strength": strength, "ts": now_ts}
    sym_bucket["dup_guard"] = {"strike": strike_i, "side": side, "strength": strength, "ts": now_ts}
    sym_bucket["last_out"] = out

    logger.debug("[HeroZeroExpiry] %s %s strike=%s strength=%s", su, sig, strike_i, strength)
    return out
