from __future__ import annotations

from typing import Any, Dict, List, Tuple


def compute_atm_strike(chain: Dict[str, Any], price: float) -> float:
    if not chain or price <= 0:
        return 0.0
    try:
        strikes = sorted(float(s) for s in chain.keys())
    except Exception:
        return 0.0
    if not strikes:
        return 0.0
    return min(strikes, key=lambda s: abs(s - price))


def strike_step(chain: Dict[str, Any]) -> float:
    try:
        strikes = sorted(float(s) for s in chain.keys())
        if len(strikes) < 2:
            return 50.0
        return abs(strikes[1] - strikes[0])
    except Exception:
        return 50.0


def volume_cluster_near_atm(
    chain: Dict[str, Dict[str, Dict[str, Any]]],
    price: float,
    max_steps: int = 3,
) -> Tuple[float, float]:
    """Sum CE and PE volume within ±max_steps of ATM (shared cache helper)."""
    atm = compute_atm_strike(chain, price)
    step = strike_step(chain)
    if atm <= 0 or step <= 0:
        return 0.0, 0.0
    ce_v = pe_v = 0.0
    for s_str, sides in chain.items():
        try:
            s_val = float(s_str)
        except Exception:
            continue
        if abs(s_val - atm) > max_steps * step:
            continue
        if not isinstance(sides, dict):
            continue
        for opt, leg in sides.items():
            if not isinstance(leg, dict):
                continue
            v = float(leg.get("volume") or 0.0)
            if opt == "CE":
                ce_v += v
            elif opt == "PE":
                pe_v += v
    return ce_v, pe_v
