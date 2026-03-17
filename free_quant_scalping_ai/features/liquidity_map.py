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

    # Support zones = strikes with highest PE OI; resistance zones = strikes with highest CE OI
    ce_oi = df[df["option_type"] == "CE"].groupby("strike")["oi"].sum()
    pe_oi = df[df["option_type"] == "PE"].groupby("strike")["oi"].sum()

    support_zones = [
        {"strike": float(s), "strength": round(min(1.0, float(pe_oi.get(s, 0)) / (oi_max + 1e-9)), 3), "oi": float(pe_oi.get(s, 0))}
        for s in pe_oi.index
        if float(pe_oi.get(s, 0)) > 0
    ]
    resistance_zones = [
        {"strike": float(s), "strength": round(min(1.0, float(ce_oi.get(s, 0)) / (oi_max + 1e-9)), 3), "oi": float(ce_oi.get(s, 0))}
        for s in ce_oi.index
        if float(ce_oi.get(s, 0)) > 0
    ]
    support_zones = sorted(support_zones, key=lambda x: -x.get("oi", 0))[:10]
    resistance_zones = sorted(resistance_zones, key=lambda x: -x.get("oi", 0))[:10]

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


def detect_liquidity_clusters(option_chain: pd.DataFrame) -> Dict[str, Any]:
    """
    Identify strikes with OI spike > 1.5 * average OI.
    support_clusters = high PE OI; resistance_clusters = high CE OI.
    stop_clusters = high volume + sudden OI drop (high vol, negative OI change).
    """
    if option_chain is None or option_chain.empty:
        return {"support_zones": [], "resistance_zones": [], "stop_clusters": []}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["volume"] = df["volume"].fillna(0)
    if "change_oi" not in df.columns and "oi_change" in df.columns:
        df["change_oi"] = df["oi_change"]
    if "change_oi" not in df.columns:
        df["change_oi"] = 0.0
    df["change_oi"] = df["change_oi"].fillna(0)

    avg_oi = df["oi"].mean() or 1.0
    oi_spike_mask = df["oi"] > 1.5 * avg_oi

    pe_oi = df[df["option_type"] == "PE"].groupby("strike")["oi"].sum()
    ce_oi = df[df["option_type"] == "CE"].groupby("strike")["oi"].sum()
    avg_vol = df["volume"].mean() or 1.0
    high_vol = df["volume"] > 1.5 * avg_vol
    oi_drop = df["change_oi"] < 0

    support_zones: List[Dict[str, Any]] = []
    for strike in pe_oi.index:
        if pe_oi[strike] > 1.5 * avg_oi:
            support_zones.append({"strike": float(strike), "oi": float(pe_oi[strike])})
    support_zones = sorted(support_zones, key=lambda x: -x["oi"])[:15]

    resistance_zones: List[Dict[str, Any]] = []
    for strike in ce_oi.index:
        if ce_oi[strike] > 1.5 * avg_oi:
            resistance_zones.append({"strike": float(strike), "oi": float(ce_oi[strike])})
    resistance_zones = sorted(resistance_zones, key=lambda x: -x["oi"])[:15]

    stop_candidates = df[high_vol & (oi_drop)]
    stop_clusters = []
    for strike in stop_candidates["strike"].unique():
        sub = stop_candidates[stop_candidates["strike"] == strike]
        if not sub.empty:
            stop_clusters.append({
                "strike": float(strike),
                "volume": float(sub["volume"].sum()),
                "oi_change": float(sub["change_oi"].sum()),
            })
    stop_clusters = sorted(stop_clusters, key=lambda x: -x["volume"])[:10]

    return {
        "support_zones": support_zones,
        "resistance_zones": resistance_zones,
        "stop_clusters": stop_clusters,
    }
