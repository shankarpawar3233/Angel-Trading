from __future__ import annotations

"""
Fast hero-zero detector.

Scans only ATM and nearby strikes (±1/±2) using the in-memory strike-keyed chain:
    { "23150": {"CE": {ltp, oi, volume, change_oi}, "PE": {...}}, ... }
"""

from typing import Dict, Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

ChainDict = Dict[str, Dict[str, Dict[str, Any]]]


def detect_hero_zero_fast(chain: ChainDict, price: float) -> Optional[Dict[str, Any]]:
    """
    Hero-zero candidate if:
      - near ATM (±2 strikes)
      - sudden OI increase (change_oi positive and relatively large)
      - volume spike
      - LTP jump > threshold vs nearby legs
    Returns {"strike", "type", "strength"} or None.
    """
    if not chain or price <= 0:
        return None

    # Find ATM strike
    try:
        strikes = sorted(float(s) for s in chain.keys())
    except Exception:
        return None
    if not strikes:
        return None
    atm = min(strikes, key=lambda s: abs(s - price))
    step = strikes[1] - strikes[0] if len(strikes) > 1 else 50.0

    # Compute global averages for normalization
    total_vol = total_oi = total_change_up = 0.0
    count = 0
    for sides in chain.values():
        for leg in sides.values():
            vol = float(leg.get("volume") or 0.0)
            oi = float(leg.get("oi") or 0.0)
            ch = float(leg.get("change_oi") or 0.0)
            total_vol += vol
            total_oi += oi
            if ch > 0:
                total_change_up += ch
            count += 1
    avg_vol = total_vol / max(1, count)
    avg_oi = total_oi / max(1, count)
    avg_change_up = total_change_up / max(1, count)

    best: Optional[Dict[str, Any]] = None

    for s_str, sides in chain.items():
        try:
            s_val = float(s_str)
        except Exception:
            continue
        if abs(s_val - atm) > 2 * step:
            continue  # outside ATM±2
        for opt_type, leg in sides.items():
            ltp = float(leg.get("ltp") or 0.0)
            vol = float(leg.get("volume") or 0.0)
            oi = float(leg.get("oi") or 0.0)
            ch = float(leg.get("change_oi") or 0.0)
            if ltp <= 0 or vol <= 0 or oi <= 0:
                continue

            # simple spikes
            vol_spike = vol > 2.0 * avg_vol
            oi_spike = (oi > 1.5 * avg_oi) or (ch > 2.0 * avg_change_up if avg_change_up > 0 else ch > 0)
            if not (vol_spike and oi_spike):
                continue

            # approximate LTP jump vs. average premium
            ltp_ratio = ltp / max(1.0, avg_vol / max(1, count))
            if ltp_ratio < 1.0:
                continue

            # strength 0–100
            strength = 0.0
            strength += min(1.0, vol / (3.0 * avg_vol if avg_vol > 0 else vol)) * 40.0
            strength += min(1.0, oi / (2.0 * avg_oi if avg_oi > 0 else oi)) * 40.0
            strength += min(1.0, abs(ch) / (3.0 * avg_change_up if avg_change_up > 0 else abs(ch))) * 20.0
            strength = min(100.0, max(0.0, strength))

            candidate = {
                "strike": s_val,
                "type": opt_type,
                "strength": int(round(strength)),
            }
            if best is None or candidate["strength"] > best["strength"]:
                best = candidate

    if best:
        logger.info("[HERO ZERO] %s%s strength=%s", int(best["strike"]), best["type"], best["strength"])
    return best

