from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def _estimate_gamma_by_strike(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    # Crude proxy: option OI * (1 / distance to underlying)
    # You should replace with a proper options model if desired.
    underlying = df["symbol"].iloc[0]
    # Assume last close as reference
    ref_price = df["ltp"].median()
    df["distance"] = (df["strike"] - ref_price).abs().clip(lower=1)
    df["gamma_proxy"] = df["oi"].fillna(0) / df["distance"]
    gamma = (
        df.groupby("strike", as_index=False)["gamma_proxy"]
        .sum()
        .sort_values("strike")
    )
    return gamma


def analyze_gamma(symbol: str, option_chain: pd.DataFrame) -> Dict:
    if option_chain.empty:
        return {"gamma_walls": [], "gamma_flip": None}

    gamma = _estimate_gamma_by_strike(option_chain)
    if gamma.empty:
        return {"gamma_walls": [], "gamma_flip": None}

    top = gamma.sort_values("gamma_proxy", ascending=False).head(3)
    walls = [
        {"strike": float(row["strike"]), "gamma": float(row["gamma_proxy"])}
        for _, row in top.iterrows()
    ]

    # Gamma flip: where cumulative gamma crosses 0 (here just median strike proxy)
    flip_idx = int(len(gamma) / 2)
    gamma_flip = float(gamma["strike"].iloc[flip_idx])

    return {"gamma_walls": walls, "gamma_flip": gamma_flip}

