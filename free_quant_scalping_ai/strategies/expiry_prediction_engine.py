from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd


def compute_max_pain(option_chain: pd.DataFrame) -> float | None:
    if option_chain.empty:
        return None
    strikes = sorted(option_chain["strike"].unique())
    total_pain = []
    for K in strikes:
        ce_pain = ((option_chain["strike"] - K).clip(lower=0) * option_chain["oi"]).sum()
        pe_pain = ((K - option_chain["strike"]).clip(lower=0) * option_chain["oi"]).sum()
        total_pain.append(ce_pain + pe_pain)
    if not total_pain:
        return None
    min_idx = int(np.argmin(total_pain))
    return float(strikes[min_idx])


def expiry_prediction(
    symbol: str,
    option_chain: pd.DataFrame,
    spot_price: float | None = None,
    gamma_walls: Dict[str, Any] | None = None,
    institutional_flow: Dict[str, Any] | None = None,
) -> Dict:
    """
    Expiry day direction model using max pain distance, PCR, gamma walls, institutional flow.
    Returns direction (BULLISH | BEARISH | RANGE), expected_range_low/high, probability.
    """
    if option_chain.empty:
        return {"prediction": None, "direction": "RANGE", "expected_range_low": None, "expected_range_high": None, "probability": 0}

    max_pain = compute_max_pain(option_chain)
    if max_pain is None:
        return {"prediction": None, "direction": "RANGE", "expected_range_low": None, "expected_range_high": None, "probability": 0}

    spot = spot_price if spot_price and spot_price > 0 else float(option_chain["strike"].median())
    max_pain_distance = spot - max_pain

    ce_oi = option_chain[option_chain["option_type"] == "CE"]["oi"].sum()
    pe_oi = option_chain[option_chain["option_type"] == "PE"]["oi"].sum()
    pcr = pe_oi / (ce_oi + 1e-9)

    strikes = option_chain["strike"].values
    weights = option_chain["oi"].fillna(0).values
    if weights.sum() <= 0:
        low, high = max_pain * 0.99, max_pain * 1.01
    else:
        mean = float((strikes * weights).sum() / weights.sum())
        var = float(((strikes - mean) ** 2 * weights).sum() / weights.sum())
        std = np.sqrt(max(var, 0))
        low, high = mean - std, mean + std

    # Direction score: max pain + PCR + gamma + flow
    score_bull = 0.0
    score_bear = 0.0
    if max_pain_distance > 0:
        score_bull += 0.3
    elif max_pain_distance < 0:
        score_bear += 0.3
    if pcr < 0.9:
        score_bull += 0.2
    elif pcr > 1.1:
        score_bear += 0.2
    gamma_res = (gamma_walls or {}).get("gamma_resistance")
    gamma_sup = (gamma_walls or {}).get("gamma_support")
    if gamma_res is not None and spot > gamma_res:
        score_bear += 0.15
    if gamma_sup is not None and spot < gamma_sup:
        score_bull += 0.15
    flow = (institutional_flow or {}).get("flow") or (institutional_flow or {}).get("flow_type") or ""
    if "LONG_BUILDUP" in flow or "SHORT_COVERING" in flow:
        score_bull += 0.2
    elif "SHORT_BUILDUP" in flow or "LONG_UNWINDING" in flow:
        score_bear += 0.2

    if score_bull > score_bear + 0.1:
        direction = "BULLISH"
        probability = min(90, int(50 + score_bull * 40))
    elif score_bear > score_bull + 0.1:
        direction = "BEARISH"
        probability = min(90, int(50 + score_bear * 40))
    else:
        direction = "RANGE"
        probability = 50

    return {
        "prediction": {
            "direction": direction,
            "expected_range": [float(low), float(high)],
            "max_pain": max_pain,
        },
        "direction": direction,
        "expected_range_low": float(low),
        "expected_range_high": float(high),
        "probability": probability,
    }

