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

from typing import Dict, Any, List, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

ChainDict = Dict[str, Dict[str, Dict[str, Any]]]


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
                if opt_type in chain.get(str(int(s), 10), {}) or opt_type in chain.get(str(s), {}):
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
        "price_momentum": 0.0,
    }
    if not chain or price <= 0:
        return features

    # Aggregate OI / volume near ATM±2
    total_call_oi = total_put_oi = 0.0
    total_call_vol = total_put_vol = 0.0
    ce_near = pe_near = 0.0
    ce_vol_near = pe_vol_near = 0.0

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
            if opt_type == "CE":
                total_call_oi += oi
                total_call_vol += vol
                if dist <= 2 * (strikes[1] - strikes[0] if len(strikes) > 1 else 50):
                    ce_near += oi
                    ce_vol_near += vol
            elif opt_type == "PE":
                total_put_oi += oi
                total_put_vol += vol
                if dist <= 2 * (strikes[1] - strikes[0] if len(strikes) > 1 else 50):
                    pe_near += oi
                    pe_vol_near += vol

    def _ratio(num: float, denom: float) -> float:
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, num / denom))

    features["call_oi_strength"] = _ratio(ce_near, total_call_oi)
    features["put_oi_strength"] = _ratio(pe_near, total_put_oi)
    features["call_volume_strength"] = _ratio(ce_vol_near, total_call_vol)
    features["put_volume_strength"] = _ratio(pe_vol_near, total_put_vol)

    # Price momentum from last N ticks (pure Python)
    if price_history:
        window = price_history[-10:]
        if len(window) >= 2:
            start = window[0]
            end = window[-1]
            if start > 0:
                momentum = (end - start) / start
                # clamp to [-1, 1]
                momentum = max(-1.0, min(1.0, momentum))
                features["price_momentum"] = momentum

    return features

