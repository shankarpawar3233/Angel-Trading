"""
Dealer Gamma Exposure (GEX) model.

Estimates dealer gamma exposure by strike: calls contribute positive gamma, puts negative.
Gamma walls = resistance (max positive gamma) and support (max negative gamma).
Gamma flip = level where net gamma crosses zero.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_gamma_exposure(
    option_chain: pd.DataFrame,
    use_oi_change_as_sensitivity: bool = False,
) -> Dict[str, Any]:
    """
    Estimate dealer gamma exposure from option chain.

    Per strike: gamma = call_oi - put_oi (calls = positive gamma, puts = negative).
    If use_oi_change_as_sensitivity: scale by oi_change as proxy for price sensitivity.

    Returns:
        {
            "gamma_levels": [{"strike": 23000, "gamma": 120000}, ...],
            "gamma_wall_call": strike with max gamma (resistance),
            "gamma_wall_put": strike with min gamma (support),
            "gamma_flip": strike where gamma crosses zero (between call wall and put wall),
        }
    """
    if option_chain is None or option_chain.empty:
        return {
            "gamma_levels": [],
            "gamma_wall_call": None,
            "gamma_wall_put": None,
            "gamma_flip": None,
        }

    df = option_chain.copy()
    if "oi" not in df.columns:
        return {"gamma_levels": [], "gamma_wall_call": None, "gamma_wall_put": None, "gamma_flip": None}
    df["oi"] = df["oi"].fillna(0)
    if use_oi_change_as_sensitivity and "change_oi" in df.columns:
        df["weight"] = 1.0 + df["change_oi"].fillna(0).abs() / (df["oi"] + 1e-9)
    else:
        df["weight"] = 1.0

    # Calls: positive gamma. Puts: negative gamma.
    df["signed_oi"] = df.apply(
        lambda r: r["oi"] * r["weight"] if r["option_type"] == "CE" else -r["oi"] * r["weight"],
        axis=1,
    )
    by_strike = df.groupby("strike", as_index=False)["signed_oi"].sum()
    by_strike = by_strike.rename(columns={"signed_oi": "gamma"})
    by_strike = by_strike.sort_values("strike")

    gamma_levels: List[Dict[str, Any]] = [
        {"strike": float(row["strike"]), "gamma": float(row["gamma"])}
        for _, row in by_strike.iterrows()
    ]

    if by_strike.empty:
        return {
            "gamma_levels": [],
            "gamma_wall_call": None,
            "gamma_wall_put": None,
            "gamma_flip": None,
        }

    max_gamma_row = by_strike.loc[by_strike["gamma"].idxmax()]
    min_gamma_row = by_strike.loc[by_strike["gamma"].idxmin()]
    gamma_wall_call = float(max_gamma_row["strike"])
    gamma_wall_put = float(min_gamma_row["strike"])

    # Gamma flip: strike where gamma crosses zero (interpolate or pick nearest to zero)
    by_strike = by_strike.sort_values("strike").reset_index(drop=True)
    sign = by_strike["gamma"].sign()
    flip_strike = None
    for i in range(len(by_strike) - 1):
        if sign.iloc[i] != sign.iloc[i + 1]:
            flip_strike = float(by_strike["strike"].iloc[i])
            break
    if flip_strike is None and not by_strike.empty:
        flip_strike = float(by_strike.loc[by_strike["gamma"].abs().idxmin(), "strike"])

    if gamma_wall_call is not None:
        logger.info("[GEX] Gamma wall detected at %s (call resistance)", gamma_wall_call)
    if gamma_wall_put is not None:
        logger.info("[GEX] Gamma wall detected at %s (put support)", gamma_wall_put)

    return {
        "gamma_levels": gamma_levels,
        "gamma_wall_call": gamma_wall_call,
        "gamma_wall_put": gamma_wall_put,
        "gamma_flip": flip_strike,
    }
