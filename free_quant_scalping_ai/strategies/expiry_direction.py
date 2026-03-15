"""
Expiry direction engine.

Combines Max Pain, gamma walls, OI clusters, and PCR to produce expiry bias and expected range.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_expiry_direction(
    spot_price: float,
    max_pain: Optional[float],
    pcr: Optional[float],
    gamma_wall_call: Optional[float],
    gamma_wall_put: Optional[float],
    option_chain: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Expiry bias from Max Pain, PCR, and gamma levels.

    Logic:
      - If spot < max_pain and PCR < 1 → BULLISH (price may move up toward max pain).
      - If spot > max_pain and PCR > 1 → BEARISH (price may move down toward max pain).
      - Otherwise use PCR and position vs gamma walls for NEUTRAL / BULLISH / BEARISH.

    Returns:
        {
            "expiry_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
            "expected_range": [low, high],
        }
    """
    expected_range: List[float] = []
    bias = "NEUTRAL"

    if max_pain is not None and spot_price > 0:
        if spot_price < max_pain and (pcr is None or pcr < 1.0):
            bias = "BULLISH"
            logger.info("[EXPIRY] Bullish expiry bias detected (spot < max_pain, PCR < 1)")
        elif spot_price > max_pain and (pcr is not None and pcr > 1.0):
            bias = "BEARISH"
            logger.info("[EXPIRY] Bearish expiry bias detected (spot > max_pain, PCR > 1)")
        elif spot_price < max_pain:
            bias = "BULLISH"
        elif spot_price > max_pain:
            bias = "BEARISH"

    # Expected range: use max_pain ± spread from gamma walls or OI std
    if max_pain is not None:
        spread = 100.0
        if gamma_wall_call is not None and gamma_wall_put is not None:
            spread = max(50.0, (gamma_wall_call - gamma_wall_put) / 2.0)
        if option_chain is not None and not option_chain.empty:
            strikes = option_chain["strike"].values
            oi = option_chain["oi"].fillna(0).values
            if oi.sum() > 0:
                mean_s = (strikes * oi).sum() / oi.sum()
                var = ((strikes - mean_s) ** 2 * oi).sum() / oi.sum()
                std = np.sqrt(max(var, 0))
                spread = max(spread, std * 0.5)
        expected_range = [max_pain - spread, max_pain + spread]
    else:
        expected_range = [spot_price * 0.99, spot_price * 1.01] if spot_price > 0 else []

    return {
        "expiry_bias": bias,
        "expected_range": expected_range,
    }
