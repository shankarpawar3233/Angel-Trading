from __future__ import annotations

from typing import Dict

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


def expiry_prediction(symbol: str, option_chain: pd.DataFrame) -> Dict:
    if option_chain.empty:
        return {"prediction": None}

    max_pain = compute_max_pain(option_chain)
    if max_pain is None:
        return {"prediction": None}

    # Simple direction proxy: compare CE vs PE OI
    ce_oi = option_chain[option_chain["option_type"] == "CE"]["oi"].sum()
    pe_oi = option_chain[option_chain["option_type"] == "PE"]["oi"].sum()
    if ce_oi < pe_oi:
        direction = "Bullish"
    elif ce_oi > pe_oi:
        direction = "Bearish"
    else:
        direction = "Neutral"

    # Expected range: +- 1 std dev of strikes weighted by OI
    strikes = option_chain["strike"].values
    weights = option_chain["oi"].fillna(0).values
    if weights.sum() <= 0:
        low, high = max_pain * 0.99, max_pain * 1.01
    else:
        mean = (strikes * weights).sum() / weights.sum()
        var = ((strikes - mean) ** 2 * weights).sum() / weights.sum()
        std = np.sqrt(var)
        low, high = mean - std, mean + std

    return {
        "prediction": {
            "direction": direction,
            "expected_range": [float(low), float(high)],
            "max_pain": max_pain,
        }
    }

