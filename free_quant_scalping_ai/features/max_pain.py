"""
Max Pain engine.

Calculates the strike at which total option writer loss is minimized at expiry
(minimum total intrinsic value of OI).
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_max_pain(option_chain: pd.DataFrame) -> Dict[str, Any]:
    """
    Max Pain: strike where total option loss (for writers) is minimum at expiry.

    For each candidate strike K:
      CE pain = sum over CE: max(0, strike - K) * OI
      PE pain = sum over PE: max(0, K - strike) * OI
      total_pain = CE_pain + PE_pain
    Max pain = K with minimum total_pain.

    Returns:
        {"max_pain": 23150}
    """
    if option_chain is None or option_chain.empty:
        return {"max_pain": None}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    strikes = sorted(df["strike"].unique())

    # At expiry settlement K: CE writer loss = sum max(0, K - strike_ce)*oi_ce; PE writer loss = sum max(0, strike_pe - K)*oi_pe
    total_pain = []
    for K in strikes:
        ce_df = df[df["option_type"] == "CE"]
        pe_df = df[df["option_type"] == "PE"]
        ce_pain = ((K - ce_df["strike"].values).clip(min=0) * ce_df["oi"].values).sum()
        pe_pain = ((pe_df["strike"].values - K).clip(min=0) * pe_df["oi"].values).sum()
        total_pain.append(ce_pain + pe_pain)

    if not total_pain:
        return {"max_pain": None}

    min_idx = int(np.argmin(total_pain))
    max_pain = float(strikes[min_idx])
    logger.info("[MAXPAIN] strike calculated: %s", max_pain)
    return {"max_pain": max_pain}
