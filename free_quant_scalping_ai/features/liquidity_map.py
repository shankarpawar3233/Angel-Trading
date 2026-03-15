"""
Liquidity map engine.

Builds liquidity heatmap from option OI, option volume, and gamma exposure.
Identifies support zones, resistance zones, and stop-loss clusters.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def build_liquidity_map(
    option_chain: pd.DataFrame,
    gamma_levels: List[Dict[str, Any]] | None = None,
    top_n_strikes: int = 15,
) -> Dict[str, Any]:
    """
    Build liquidity heatmap from option OI, volume, and gamma.

    option_chain: DataFrame with strike, option_type, ltp, volume, oi.
    gamma_levels: optional list of {"strike", "gamma"} from compute_gamma_exposure.

    Returns:
        {
            "heatmap": [{"strike": 23150, "oi": ..., "volume": ..., "gamma": ..., "score": ...}, ...],
            "support_zones": [{"strike": 23000, "strength": 0.8}, ...],
            "resistance_zones": [{"strike": 23300, "strength": 0.9}, ...],
            "stop_loss_clusters": [{"strike": 23100, "oi_density": ...}, ...],
        }
    """
    if option_chain is None or option_chain.empty:
        return {
            "heatmap": [],
            "support_zones": [],
            "resistance_zones": [],
            "stop_loss_clusters": [],
        }

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["volume"] = df["volume"].fillna(0)

    gamma_by_strike: Dict[float, float] = {}
    if gamma_levels:
        for g in gamma_levels:
            s = g.get("strike")
            if s is not None:
                gamma_by_strike[float(s)] = float(g.get("gamma", 0))

    by_strike = df.groupby("strike").agg(
        oi=("oi", "sum"),
        volume=("volume", "sum"),
    ).reset_index()

    by_strike["gamma"] = by_strike["strike"].map(lambda s: gamma_by_strike.get(s, 0.0))
    oi_max = by_strike["oi"].max() or 1.0
    vol_max = by_strike["volume"].max() or 1.0
    gamma_abs_max = by_strike["gamma"].abs().max() or 1.0
    by_strike["score"] = (
        0.4 * (by_strike["oi"] / oi_max)
        + 0.3 * (by_strike["volume"] / vol_max)
        + 0.3 * (by_strike["gamma"].abs() / gamma_abs_max)
    )

    heatmap = []
    for _, row in by_strike.nlargest(top_n_strikes * 2, "score").iterrows():
        heatmap.append({
            "strike": float(row["strike"]),
            "oi": float(row["oi"]),
            "volume": float(row["volume"]),
            "gamma": float(row["gamma"]),
            "score": round(float(row["score"]), 4),
        })

    # Support = high put OI / negative gamma below spot; resistance = high call OI / positive gamma above
    ce_oi = df[df["option_type"] == "CE"].groupby("strike")["oi"].sum()
    pe_oi = df[df["option_type"] == "PE"].groupby("strike")["oi"].sum()

    support_zones: List[Dict[str, Any]] = []
    resistance_zones: List[Dict[str, Any]] = []
    for strike in sorted(by_strike["strike"].unique()):
        pe = float(pe_oi.get(strike, 0))
        ce = float(ce_oi.get(strike, 0))
        g = gamma_by_strike.get(strike, 0.0)
        strength = min(1.0, (pe + ce) / (oi_max + 1e-9))
        if pe > ce and g <= 0:
            support_zones.append({"strike": float(strike), "strength": round(strength, 3)})
        elif ce > pe and g >= 0:
            resistance_zones.append({"strike": float(strike), "strength": round(strength, 3)})

    support_zones = sorted(support_zones, key=lambda x: -x["strength"])[:10]
    resistance_zones = sorted(resistance_zones, key=lambda x: -x["strength"])[:10]

    # Stop-loss clusters: strikes with very high OI (likely stop clusters)
    by_strike_sorted = by_strike.sort_values("oi", ascending=False).head(top_n_strikes)
    stop_loss_clusters = [
        {"strike": float(row["strike"]), "oi_density": float(row["oi"])}
        for _, row in by_strike_sorted.iterrows()
    ]

    return {
        "heatmap": heatmap,
        "support_zones": support_zones,
        "resistance_zones": resistance_zones,
        "stop_loss_clusters": stop_loss_clusters,
    }
