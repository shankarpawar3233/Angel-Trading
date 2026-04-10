from __future__ import annotations

"""
Lightweight, pandas-free feature engine for low-latency scalping.

Works directly on the in-memory strike-keyed option chain:
    {
        "23150": {"CE": {ltp, oi, volume, change_oi}, "PE": {...}},
        ...
    }

and a short in-memory price history (simple Python list).
"""

from typing import Any, Dict, List, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

ChainDict = Dict[str, Dict[str, Dict[str, Any]]]


def _leg_ltp(leg: Any) -> float:
    if not isinstance(leg, dict):
        return 0.0
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
    return 0.0


def _nearest_strikes(chain: ChainDict, price: float, max_offsets: int = 2) -> List[Tuple[float, str]]:
    """Return list of (strike, type) for ATM±offset up to max_offsets."""
    if not chain or price <= 0:
        return []
    try:
        strikes = sorted(float(s) for s in chain.keys())
    except Exception:
        return []
    if not strikes:
        return []
    # Find ATM index
    atm_strike = min(strikes, key=lambda s: abs(s - price))
    idx = strikes.index(atm_strike)
    out: List[Tuple[float, str]] = []
    for offset in range(-max_offsets, max_offsets + 1):
        j = idx + offset
        if 0 <= j < len(strikes):
            s = strikes[j]
            for opt_type in ("CE", "PE"):
                if opt_type in chain.get(str(int(s)), {}) or opt_type in chain.get(str(s), {}):
                    out.append((s, opt_type))
    return out


def compute_fast_features(
    chain: ChainDict,
    price: float,
    price_history: List[float],
) -> Dict[str, Any]:
    """
    Compute ultra-fast features:
      - call/put OI spike around ATM
      - call/put volume spike around ATM
      - short-horizon price momentum
    """
    features: Dict[str, Any] = {
        "call_oi_strength": 0.0,
        "put_oi_strength": 0.0,
        "call_volume_strength": 0.0,
        "put_volume_strength": 0.0,
        "call_oi_change_strength": 0.0,
        "put_oi_change_strength": 0.0,
        "spread_skew": 0.0,
        "volume_available": False,
        "price_momentum": 0.0,
    }
    if not chain or price <= 0:
        return features

    # Aggregate OI / volume / |OI change| near ATM±3 steps
    total_call_oi = total_put_oi = 0.0
    total_call_vol = total_put_vol = 0.0
    total_call_chg = total_put_chg = 0.0
    ce_near = pe_near = 0.0
    ce_vol_near = pe_vol_near = 0.0
    ce_chg_near = pe_chg_near = 0.0

    try:
        strikes = [float(s) for s in chain.keys()]
    except Exception:
        strikes = []
    if not strikes:
        return features
    atm = min(strikes, key=lambda s: abs(s - price))

    for s_str, sides in chain.items():
        try:
            s_val = float(s_str)
        except Exception:
            continue
        dist = abs(s_val - atm)
        for opt_type, leg in sides.items():
            oi = float(leg.get("oi") or 0.0)
            vol = float(leg.get("volume") or 0.0)
            chg = float(leg.get("change_oi") or leg.get("oi_change") or 0.0)
            chg_abs = abs(chg)
            if opt_type == "CE":
                total_call_oi += oi
                total_call_vol += vol
                total_call_chg += chg_abs
                if dist <= 3 * (strikes[1] - strikes[0] if len(strikes) > 1 else 50):
                    ce_near += oi
                    ce_vol_near += vol
                    ce_chg_near += chg_abs
            elif opt_type == "PE":
                total_put_oi += oi
                total_put_vol += vol
                total_put_chg += chg_abs
                if dist <= 3 * (strikes[1] - strikes[0] if len(strikes) > 1 else 50):
                    pe_near += oi
                    pe_vol_near += vol
                    pe_chg_near += chg_abs

    def _ratio(num: float, denom: float) -> float:
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, num / denom))

    features["call_oi_strength"] = _ratio(ce_near, total_call_oi)
    features["put_oi_strength"] = _ratio(pe_near, total_put_oi)
    features["call_volume_strength"] = _ratio(ce_vol_near, total_call_vol)
    features["put_volume_strength"] = _ratio(pe_vol_near, total_put_vol)
    features["call_oi_change_strength"] = _ratio(ce_chg_near, total_call_chg)
    features["put_oi_change_strength"] = _ratio(pe_chg_near, total_put_chg)
    features["volume_available"] = (total_call_vol + total_put_vol) > 0.0

    # ATM CE vs PE premium skew: positive => calls richer (often bullish flow / squeeze)
    try:
        row = chain.get(str(int(atm))) or chain.get(str(atm)) or {}
        if isinstance(row, dict):
            ce_leg = row.get("CE") or {}
            pe_leg = row.get("PE") or {}
            ce_l = _leg_ltp(ce_leg)
            pe_l = _leg_ltp(pe_leg)
            den = ce_l + pe_l
            if den > 0:
                features["spread_skew"] = max(-1.0, min(1.0, (ce_l - pe_l) / den))
    except Exception:
        pass

    # Price momentum: blend short (last ~5s) + longer window (~24–48 ticks) so slow
    # sustained trends are visible; pure 10-tick mom often reads ~0 during gradual drift.
    if price_history:

        def _mom(w: List[float]) -> float:
            if len(w) < 2:
                return 0.0
            a, b = w[0], w[-1]
            if a <= 0:
                return 0.0
            m = (b - a) / a
            return max(-1.0, min(1.0, m))

        w_short = price_history[-10:]
        mom_s = _mom(w_short)
        if len(price_history) >= 48:
            w_long = price_history[-48:]
        elif len(price_history) >= 24:
            w_long = price_history[-24:]
        else:
            w_long = w_short
        mom_l = _mom(w_long)
        if len(price_history) >= 24:
            # Weight longer path so "same direction since long" shows up in features.
            blended = 0.35 * mom_s + 0.65 * mom_l
            features["price_momentum"] = max(-1.0, min(1.0, blended))
        else:
            features["price_momentum"] = mom_s

    return features

