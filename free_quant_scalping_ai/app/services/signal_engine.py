"""
Live scalping signal pipeline: rule/ML merge, filters, hysteresis, entry stabilizer.

Thresholds align with product defaults:
  - NIFTY trend/vol gates: ~8–12 index points where applicable
  - SENSEX: ~25–40 index points (wider index scale)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

SIGNAL_DEBUG = os.getenv("SIGNAL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


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


def merge_ml_fallback(scalping_signal: str, ml_signal: Dict[str, Any], confidence: int) -> Tuple[str, int]:
    if scalping_signal == "NO_TRADE" and ml_signal.get("label") in ("BUY_CE", "BUY_PE"):
        floor = _env_int("ML_CONFIDENCE_FLOOR", 55)
        if int(ml_signal.get("confidence") or 0) >= floor:
            return str(ml_signal["label"]), max(confidence, int(ml_signal["confidence"]) - 4)
    return scalping_signal, confidence


def sensex_premium_fallback(
    su: str,
    scalping_signal: str,
    chain_snapshot: Dict[str, Any],
    price: float,
    features: Dict[str, Any],
    confidence: int,
) -> Tuple[str, int]:
    if su != "SENSEX" or scalping_signal != "NO_TRADE" or not chain_snapshot:
        return scalping_signal, confidence
    try:
        strikes = sorted(float(s) for s in chain_snapshot.keys())
        if not strikes:
            return scalping_signal, confidence
        atm = min(strikes, key=lambda s: abs(s - price))
        row = chain_snapshot.get(str(int(atm))) or chain_snapshot.get(str(atm)) or {}
        ce = row.get("CE") if isinstance(row, dict) else None
        pe = row.get("PE") if isinstance(row, dict) else None
        ce_ltp = float((ce or {}).get("ltp") or 0.0)
        pe_ltp = float((pe or {}).get("ltp") or 0.0)
        mom = float(features.get("price_momentum") or 0.0)
        if ce_ltp > 0 and pe_ltp > 0:
            ratio = pe_ltp / max(1.0, ce_ltp)
            if ratio >= 1.35 and mom <= 0.0002:
                return "BUY_PE", max(confidence, 62)
            if ratio <= 0.74 and mom >= -0.0002:
                return "BUY_CE", max(confidence, 62)
    except Exception:
        pass
    return scalping_signal, confidence


def bias_tiebreak(
    su: str,
    scalping_signal: str,
    confidence: int,
    call_oi: float,
    put_oi: float,
    call_vol: float,
    put_vol: float,
    mom: float,
) -> Tuple[str, int]:
    if scalping_signal != "NO_TRADE":
        return scalping_signal, confidence
    ce_bias = 0.45 * call_oi + 0.45 * call_vol + 0.10 * max(0.0, mom * 2000.0)
    pe_bias = 0.45 * put_oi + 0.45 * put_vol + 0.10 * max(0.0, -mom * 2000.0)
    bias_gap = abs(ce_bias - pe_bias)
    min_conf = _env_float("BIAS_TIEBREAK_MIN_CONF", 62.0)
    min_gap = _env_float("BIAS_TIEBREAK_MIN_GAP", 0.05)
    if confidence >= min_conf and bias_gap >= min_gap:
        if ce_bias > pe_bias:
            return "BUY_CE", max(confidence, _env_int("BIAS_TIEBREAK_CONF_CE", 66))
        return "BUY_PE", max(confidence, _env_int("BIAS_TIEBREAK_CONF_PE", 66))
    return scalping_signal, confidence


def apply_trend_filter(su: str, scalping_signal: str, sym_hist: List[float], debug: List[str]) -> str:
    trend = 0.0
    if len(sym_hist) >= 24:
        recent = sym_hist[-12:]
        prior = sym_hist[-24:-12]
        if prior:
            trend = (sum(recent) / len(recent)) - (sum(prior) / len(prior))
    if su == "SENSEX":
        trend_block = _env_float("TREND_BLOCK_POINTS_SENSEX", 30.0)
    else:
        trend_block = _env_float("TREND_BLOCK_POINTS_NIFTY", 10.0)
    if scalping_signal == "BUY_CE" and trend < -trend_block:
        debug.append(f"trend_blocks_ce|trend={trend:.2f}|block={trend_block}")
        return "NO_TRADE"
    if scalping_signal == "BUY_PE" and trend > trend_block:
        debug.append(f"trend_blocks_pe|trend={trend:.2f}|block={trend_block}")
        return "NO_TRADE"
    return scalping_signal


def apply_volatility_filter(su: str, scalping_signal: str, sym_hist: List[float], price: float, debug: List[str]) -> str:
    if len(sym_hist) < 20 or scalping_signal not in ("BUY_CE", "BUY_PE"):
        return scalping_signal
    win = sym_hist[-20:]
    mid = max(1.0, float(price or 0.0))
    vol_pct = (max(win) - min(win)) / mid
    min_vol = _env_float("MIN_RANGE_FRAC_SENSEX", 0.0009) if su == "SENSEX" else _env_float("MIN_RANGE_FRAC_NIFTY", 0.00045)
    if vol_pct < min_vol:
        debug.append(f"vol_too_flat|vol_pct={vol_pct:.6f}|min={min_vol}")
        return "NO_TRADE"
    return scalping_signal


def apply_side_balance_filter(
    symbol: str,
    scalping_signal: str,
    call_oi: float,
    put_oi: float,
    call_vol: float,
    put_vol: float,
    bal_state: Dict[str, Dict[str, float]],
    debug: List[str],
) -> str:
    sb = bal_state.setdefault(symbol, {"ce": 0.0, "pe": 0.0})
    sb["ce"] = float(sb.get("ce", 0.0)) * 0.97
    sb["pe"] = float(sb.get("pe", 0.0)) * 0.97
    if scalping_signal == "BUY_CE":
        sb["ce"] += 1.0
    elif scalping_signal == "BUY_PE":
        sb["pe"] += 1.0
    skew = sb["pe"] - sb["ce"]
    edge_gap = abs((call_oi + call_vol) - (put_oi + put_vol))
    skew_lim = _env_float("SIDE_BALANCE_SKEW", 10.0)
    edge_lim = _env_float("SIDE_BALANCE_EDGE_GAP", 0.12)
    if scalping_signal == "BUY_PE" and skew > skew_lim and edge_gap < edge_lim:
        debug.append(f"side_balance_block_pe|skew={skew:.2f}|edge_gap={edge_gap:.3f}")
        return "NO_TRADE"
    if scalping_signal == "BUY_CE" and skew < -skew_lim and edge_gap < edge_lim:
        debug.append(f"side_balance_block_ce|skew={skew:.2f}|edge_gap={edge_gap:.3f}")
        return "NO_TRADE"
    return scalping_signal


def apply_hysteresis(
    su: str,
    scalping_signal: str,
    confidence: int,
    ds: Dict[str, Any],
    now_sec: float,
) -> Tuple[str, int]:
    hold = _env_float("SIGNAL_HOLD_SEC_SENSEX", 12.0) if su == "SENSEX" else _env_float("SIGNAL_HOLD_SEC_NIFTY", 10.0)
    if scalping_signal in ("BUY_CE", "BUY_PE"):
        ds["last_directional"] = scalping_signal
        ds["last_directional_ts"] = now_sec
    elif scalping_signal == "NO_TRADE":
        last_dir = ds.get("last_directional")
        last_ts = float(ds.get("last_directional_ts") or 0.0)
        if last_dir in ("BUY_CE", "BUY_PE") and (now_sec - last_ts) <= hold:
            return str(last_dir), max(confidence, 58 if su == "SENSEX" else 62)
    return scalping_signal, confidence


def stabilizer_params(su: str, symbol: str) -> Tuple[int, int, float]:
    min_conf = _env_int("MIN_CONF_SENSEX", 58) if su == "SENSEX" else _env_int("MIN_CONF_NIFTY", 62)
    stable_cycles = _env_int("STABLE_CYCLES_SENSEX", 2) if su == "SENSEX" else _env_int("STABLE_CYCLES_NIFTY", 3)
    default_lock = "3.0" if symbol.upper() == "NIFTY" else "5.0"
    try:
        entry_lock = float(os.getenv(f"ENTRY_LOCK_SEC_{su}", default_lock))
    except ValueError:
        entry_lock = 3.0 if symbol.upper() == "NIFTY" else 5.0
    return min_conf, stable_cycles, entry_lock


def apply_entry_stabilizer(
    *,
    su: str,
    symbol: str,
    scalping_signal: str,
    confidence: int,
    price: float,
    prev_market_price: Optional[float],
    ds: Dict[str, Any],
    now_sec: float,
    debug: List[str],
) -> Tuple[str, str, int, float]:
    """
    Returns: entry_decision, decision_reason, stable_count, lock_remaining_sec
    Mutates ds (stable_count, last_signal, lock_until, active_trade).
    """
    MIN_CONF, STABLE_CYCLES, ENTRY_LOCK_SEC = stabilizer_params(su, symbol)

    if scalping_signal in ("BUY_CE", "BUY_PE"):
        if ds.get("last_signal") == scalping_signal:
            ds["stable_count"] = int(ds.get("stable_count") or 0) + 1
        else:
            ds["stable_count"] = 1
    else:
        ds["stable_count"] = 0
    ds["last_signal"] = scalping_signal

    if float(ds.get("lock_until") or 0.0) <= now_sec:
        ds["active_trade"] = None

    locked = float(ds.get("lock_until") or 0.0) > now_sec
    lock_remaining = max(0.0, float(ds.get("lock_until") or 0.0) - now_sec)
    stable_count = int(ds.get("stable_count") or 0)

    entry_decision = "HOLD"
    decision_reason = "no_trade_signal"

    lock_move = _env_float("LOCK_PRICE_MOVE_SENSEX", 30.0) if su == "SENSEX" else _env_float("LOCK_PRICE_MOVE_NIFTY", 10.0)
    prev_price = float(prev_market_price) if prev_market_price is not None else price
    high_conf_unlock = (
        scalping_signal in ("BUY_CE", "BUY_PE")
        and confidence >= (_env_int("HIGH_CONF_UNLOCK_NIFTY", 78) if su == "NIFTY" else _env_int("HIGH_CONF_UNLOCK_SENSEX", 75))
        and ds.get("last_signal") == scalping_signal
    )
    same_as_active = ds.get("active_trade") and scalping_signal == ds.get("active_trade")
    continuation_ok = (
        locked
        and same_as_active
        and scalping_signal in ("BUY_CE", "BUY_PE")
        and stable_count >= STABLE_CYCLES
        and confidence >= MIN_CONF
    )

    if locked:
        if continuation_ok:
            entry_decision = scalping_signal
            decision_reason = "continuation_same_leg"
            debug.append("entry_continuation_while_lock")
        elif scalping_signal in ("BUY_CE", "BUY_PE") and (abs(price - prev_price) >= lock_move or high_conf_unlock):
            entry_decision = scalping_signal
            decision_reason = "reentry_on_momentum" if not high_conf_unlock else "reentry_high_conf"
            ds["active_trade"] = scalping_signal
            ds["lock_until"] = now_sec + ENTRY_LOCK_SEC
            lock_remaining = ENTRY_LOCK_SEC
            debug.append(decision_reason)
        elif scalping_signal not in ("BUY_CE", "BUY_PE"):
            decision_reason = "signal_not_directional"
        else:
            decision_reason = f"entry_lock_active_{int(round(lock_remaining))}s"
            debug.append(decision_reason)
    elif scalping_signal not in ("BUY_CE", "BUY_PE"):
        decision_reason = "signal_not_directional"
    elif confidence < MIN_CONF:
        decision_reason = f"low_confidence_{confidence}_lt_{MIN_CONF}"
        debug.append(decision_reason)
    elif stable_count < STABLE_CYCLES:
        decision_reason = f"unstable_signal_{stable_count}_lt_{STABLE_CYCLES}"
        debug.append(decision_reason)
    else:
        entry_decision = scalping_signal
        decision_reason = f"confirmed_{stable_count}_cycles_conf_{confidence}"
        ds["active_trade"] = scalping_signal
        ds["lock_until"] = now_sec + ENTRY_LOCK_SEC
        lock_remaining = ENTRY_LOCK_SEC
        debug.append("entry_confirmed_new_lock")

    return entry_decision, decision_reason, stable_count, lock_remaining


def format_debug_payload(debug: List[str], features: Dict[str, Any], thresholds: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reasons": list(debug),
        "volume_fallback_used": bool(features.get("volume_fallback_used")),
        "volume_available": features.get("volume_available"),
        "features": {
            "call_oi_strength": features.get("call_oi_strength"),
            "put_oi_strength": features.get("put_oi_strength"),
            "call_volume_strength": features.get("call_volume_strength"),
            "put_volume_strength": features.get("put_volume_strength"),
            "call_oi_change_strength": features.get("call_oi_change_strength"),
            "put_oi_change_strength": features.get("put_oi_change_strength"),
            "spread_skew": features.get("spread_skew"),
            "price_momentum": features.get("price_momentum"),
        },
        "thresholds": thresholds,
    }
