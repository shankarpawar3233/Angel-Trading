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
    price: float | None = None,
    use_oi_change_as_sensitivity: bool = False,
) -> Dict[str, Any]:
    """
    Estimate dealer gamma exposure from option chain.

    When price is provided, uses simple gamma proxy: gamma = oi / abs(strike - price + 1).
    Otherwise: per-strike gamma = call_oi - put_oi (calls positive, puts negative).

    Returns:
        gamma_levels, gamma_wall_call / call_wall, gamma_wall_put / put_wall, gamma_flip.
    """
    if option_chain is None or option_chain.empty:
        return {
            "gamma_levels": [],
            "gamma_wall_call": None,
            "gamma_wall_put": None,
            "call_wall": None,
            "put_wall": None,
            "gamma_flip": None,
        }

    df = option_chain.copy()
    if "oi" not in df.columns:
        return {
            "gamma_levels": [], "gamma_wall_call": None, "gamma_wall_put": None,
            "call_wall": None, "put_wall": None, "gamma_flip": None,
        }
    df["oi"] = df["oi"].fillna(0)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return {
            "gamma_levels": [], "gamma_wall_call": None, "gamma_wall_put": None,
            "call_wall": None, "put_wall": None, "gamma_flip": None,
        }

    spot = price if price is not None and price > 0 else float(df["strike"].median())

    if price is not None and price > 0:
        # Simple gamma proxy: gamma = oi / abs(strike - price + 1)
        df["gamma_proxy"] = df["oi"] / (df["strike"] - spot).abs().add(1).clip(lower=0.01)
        df["signed_oi"] = df.apply(
            lambda r: r["gamma_proxy"] if r["option_type"] == "CE" else -r["gamma_proxy"],
            axis=1,
        )
    else:
        if use_oi_change_as_sensitivity and "change_oi" in df.columns:
            df["weight"] = 1.0 + df["change_oi"].fillna(0).abs() / (df["oi"] + 1e-9)
        else:
            df["weight"] = 1.0
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

    # call_gamma_by_strike / put_gamma_by_strike (for compatibility)
    ce_gamma = df[df["option_type"] == "CE"].groupby("strike")["signed_oi"].sum()
    pe_gamma = df[df["option_type"] == "PE"].groupby("strike")["signed_oi"].sum()

    gamma_wall_call = float(ce_gamma.idxmax()) if not ce_gamma.empty and ce_gamma.max() > 0 else None
    gamma_wall_put = float(pe_gamma.idxmin()) if not pe_gamma.empty and pe_gamma.min() < 0 else None
    if by_strike.empty:
        gamma_wall_call = gamma_wall_put = None
    else:
        if gamma_wall_call is None:
            gamma_wall_call = float(by_strike.loc[by_strike["gamma"].idxmax(), "strike"])
        if gamma_wall_put is None:
            gamma_wall_put = float(by_strike.loc[by_strike["gamma"].idxmin(), "strike"])

    call_wall = gamma_wall_call
    put_wall = gamma_wall_put
    flip_strike = None
    by_strike_asc = by_strike.sort_values("strike").reset_index(drop=True)
    for i in range(len(by_strike_asc) - 1):
        if by_strike_asc["gamma"].iloc[i] * by_strike_asc["gamma"].iloc[i + 1] <= 0:
            flip_strike = float(by_strike_asc["strike"].iloc[i])
            break
    if flip_strike is None and not by_strike_asc.empty:
        flip_strike = float(by_strike_asc.loc[by_strike_asc["gamma"].abs().idxmin(), "strike"])

    logger.info("[GAMMA] gamma walls detected: call_wall=%s put_wall=%s", call_wall, put_wall)

    return {
        "gamma_levels": gamma_levels,
        "gamma_wall_call": gamma_wall_call,
        "gamma_wall_put": gamma_wall_put,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": flip_strike,
    }


def compute_gamma_walls(option_chain: pd.DataFrame) -> Dict[str, Any]:
    """
    Gamma wall detection: gamma_exposure = oi * strike_distance_factor.
    Top 3 strikes by gamma; gamma_resistance = highest CE gamma, gamma_support = highest PE gamma.
    Gamma flip where call gamma ≈ put gamma.
    Returns: gamma_resistance, gamma_support, gamma_flip.
    """
    if option_chain is None or option_chain.empty or "oi" not in option_chain.columns:
        return {"gamma_resistance": None, "gamma_support": None, "gamma_flip": None}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return {"gamma_resistance": None, "gamma_support": None, "gamma_flip": None}

    spot = float(df["strike"].median())
    # Strike distance factor: weight by distance from spot (closer = more reactive)
    df["strike_distance_factor"] = 1.0 / (1.0 + (df["strike"] - spot).abs() / (spot + 1e-9) * 10)
    df["gamma_exposure"] = df["oi"] * df["strike_distance_factor"]
    df["gamma_exposure"] = df.apply(
        lambda r: r["gamma_exposure"] if r["option_type"] == "CE" else -r["gamma_exposure"],
        axis=1,
    )

    by_strike = df.groupby("strike", as_index=False)["gamma_exposure"].sum()
    by_strike = by_strike.reindex(by_strike["gamma_exposure"].abs().sort_values(ascending=False).index)
    ce_strikes = df[df["option_type"] == "CE"].groupby("strike")["gamma_exposure"].sum()
    pe_strikes = df[df["option_type"] == "PE"].groupby("strike")["gamma_exposure"].sum()

    gamma_resistance = None
    gamma_support = None
    if not ce_strikes.empty:
        gamma_resistance = float(ce_strikes.idxmax())
    if not pe_strikes.empty:
        gamma_support = float(pe_strikes.idxmin())

    by_strike_asc = by_strike.sort_values("strike").reset_index(drop=True)
    gamma_flip = None
    for i in range(len(by_strike_asc) - 1):
        if by_strike_asc["gamma_exposure"].iloc[i] * by_strike_asc["gamma_exposure"].iloc[i + 1] <= 0:
            gamma_flip = float(by_strike_asc["strike"].iloc[i])
            break
    if gamma_flip is None and not by_strike_asc.empty:
        gamma_flip = float(by_strike_asc.loc[by_strike_asc["gamma_exposure"].abs().idxmin(), "strike"])

    if gamma_resistance is not None or gamma_support is not None:
        logger.info("[GAMMA] walls detected resistance=%s support=%s flip=%s", gamma_resistance, gamma_support, gamma_flip)

    return {
        "gamma_resistance": gamma_resistance,
        "gamma_support": gamma_support,
        "gamma_flip": gamma_flip,
    }


def detect_gamma_squeeze(option_chain: pd.DataFrame, price: float) -> Dict[str, Any]:
    """
    Identify strikes with extremely high CE OI near ATM. If price approaching that strike
    and gamma exposure high → predict squeeze. Return squeeze_direction UP | DOWN | NONE,
    squeeze_strike, probability 0-100.
    """
    if option_chain is None or option_chain.empty or price <= 0 or "oi" not in option_chain.columns:
        return {"squeeze_direction": "NONE", "squeeze_strike": None, "probability": 0}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return {"squeeze_direction": "NONE", "squeeze_strike": None, "probability": 0}

    atm_band = price * 0.02
    near_atm = df[(df["strike"] >= price - atm_band) & (df["strike"] <= price + atm_band)]
    ce_near = near_atm[near_atm["option_type"] == "CE"]
    if ce_near.empty:
        return {"squeeze_direction": "NONE", "squeeze_strike": None, "probability": 0}

    avg_oi = df["oi"].mean() or 1.0
    high_ce_oi = ce_near[ce_near["oi"] > 2.0 * avg_oi].sort_values("oi", ascending=False)
    if high_ce_oi.empty:
        return {"squeeze_direction": "NONE", "squeeze_strike": None, "probability": 0}

    squeeze_strike = float(high_ce_oi["strike"].iloc[0])
    dist = abs(price - squeeze_strike) / (price + 1e-9) * 100
    if price < squeeze_strike:
        squeeze_direction = "UP"
        probability = min(90, int(40 + 30 * (1.0 - dist / 2.0)))
    elif price > squeeze_strike:
        squeeze_direction = "DOWN"
        probability = min(90, int(40 + 30 * (1.0 - dist / 2.0)))
    else:
        squeeze_direction = "NONE"
        probability = 50

    logger.info("[GAMMA] gamma squeeze detection: %s strike=%s prob=%s", squeeze_direction, squeeze_strike, probability)
    return {"squeeze_direction": squeeze_direction, "squeeze_strike": squeeze_strike, "probability": probability}
