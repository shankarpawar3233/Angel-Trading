"""
Market maker (dealer) positioning model.

Estimates dealer gamma exposure from option chain: net gamma = oi * (1 / distance_from_spot).
Determines if dealers are long or short gamma.
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_market_maker_position(option_chain: pd.DataFrame, spot: float | None = None) -> Dict[str, Any]:
    """
    Estimate dealer positioning from option chain.
    gamma = oi * (1 / distance_from_spot). Aggregate call_gamma_total, put_gamma_total.
    call_gamma >> put_gamma → dealers short gamma; put_gamma >> call_gamma → dealers long gamma.
    Returns dealer_position (LONG_GAMMA | SHORT_GAMMA), gamma_pressure score 0-100.
    """
    if option_chain is None or option_chain.empty or "oi" not in option_chain.columns:
        return {"dealer_position": None, "gamma_pressure": 0}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return {"dealer_position": None, "gamma_pressure": 0}

    if spot is None or spot <= 0:
        spot = float(df["strike"].median())
    distance = (df["strike"] - spot).abs() + 1.0
    df["inv_dist"] = 1.0 / distance
    df["gamma"] = df["oi"] * df["inv_dist"]
    df["gamma"] = df.apply(lambda r: r["gamma"] if r["option_type"] == "CE" else -r["gamma"], axis=1)

    ce_df = df[df["option_type"] == "CE"]
    pe_df = df[df["option_type"] == "PE"]
    call_gamma_total = float((ce_df["oi"] * ce_df["inv_dist"]).sum()) if not ce_df.empty else 0.0
    put_gamma_total = float((pe_df["oi"] * pe_df["inv_dist"]).sum()) if not pe_df.empty else 0.0

    total = call_gamma_total + put_gamma_total
    if total <= 0:
        return {"dealer_position": "SHORT_GAMMA", "gamma_pressure": 50}

    if call_gamma_total > put_gamma_total * 1.2:
        dealer_position = "SHORT_GAMMA"
        gamma_pressure = min(100, int(50 + (call_gamma_total - put_gamma_total) / (total + 1e-9) * 50))
    elif put_gamma_total > call_gamma_total * 1.2:
        dealer_position = "LONG_GAMMA"
        gamma_pressure = min(100, int(50 + (put_gamma_total - call_gamma_total) / (total + 1e-9) * 50))
    else:
        dealer_position = "SHORT_GAMMA"
        gamma_pressure = 50

    logger.info("[MM] market maker positioning: %s (pressure %s)", dealer_position, gamma_pressure)
    return {"dealer_position": dealer_position, "gamma_pressure": gamma_pressure}
