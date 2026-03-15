from __future__ import annotations

"""
OI (Open Interest) analysis: OI change per strike, volume spikes, PCR, OI clusters.
"""

from typing import Any, Dict, List

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_oi_analysis(
    option_chain: pd.DataFrame,
    volume_spike_multiplier: float = 3.0,
    oi_spike_pct_threshold: float = 0.05,
) -> Dict[str, Any]:
    """
    Compute OI change per strike, volume spike detection, PCR ratio, OI clusters.

    option_chain must have columns: strike, option_type, ltp, volume, oi, change_oi (or oi_change).
    Optional: ts, symbol, expiry.

    Returns:
        {
            "pcr": float,
            "max_oi_call": float (strike with max call OI),
            "max_oi_put": float (strike with max put OI),
            "oi_spikes": [{"strike", "option_type", "oi_change", "volume", ...}],
            "ce_oi_total": float,
            "pe_oi_total": float,
        }
    """
    if option_chain is None or option_chain.empty:
        return {
            "pcr": 0.0,
            "max_oi_call": None,
            "max_oi_put": None,
            "oi_spikes": [],
            "ce_oi_total": 0.0,
            "pe_oi_total": 0.0,
        }

    df = option_chain.copy()
    if "change_oi" not in df.columns and "oi_change" in df.columns:
        df["change_oi"] = df["oi_change"]
    if "change_oi" not in df.columns:
        df["change_oi"] = 0.0

    # PCR: put OI / call OI
    ce_oi = df.loc[df["option_type"] == "CE", "oi"].sum()
    pe_oi = df.loc[df["option_type"] == "PE", "oi"].sum()
    ce_oi = float(ce_oi) if pd.notna(ce_oi) else 0.0
    pe_oi = float(pe_oi) if pd.notna(pe_oi) else 0.0
    pcr = pe_oi / (ce_oi + 1e-9)

    # Max OI strike for CE and PE
    ce_df = df[df["option_type"] == "CE"]
    pe_df = df[df["option_type"] == "PE"]
    max_oi_call = float(ce_df.loc[ce_df["oi"].idxmax(), "strike"]) if not ce_df.empty and ce_df["oi"].max() else None
    max_oi_put = float(pe_df.loc[pe_df["oi"].idxmax(), "strike"]) if not pe_df.empty and pe_df["oi"].max() else None
    if max_oi_call is not None and (pd.isna(max_oi_call) or ce_df["oi"].max() == 0):
        max_oi_call = None
    if max_oi_put is not None and (pd.isna(max_oi_put) or pe_df["oi"].max() == 0):
        max_oi_put = None

    # Volume spike: volume > multiplier * rolling mean (or mean)
    volume_spikes: List[Dict[str, Any]] = []
    if "volume" in df.columns:
        vol_mean = df["volume"].mean()
        if pd.isna(vol_mean) or vol_mean == 0:
            vol_mean = 1.0
        df["vol_spike"] = df["volume"] > (volume_spike_multiplier * vol_mean)
        for _, row in df[df["vol_spike"]].iterrows():
            volume_spikes.append({
                "strike": float(row["strike"]),
                "option_type": str(row["option_type"]),
                "volume": float(row["volume"]) if pd.notna(row["volume"]) else None,
                "ltp": float(row["ltp"]) if "ltp" in row and pd.notna(row.get("ltp")) else None,
            })
    else:
        df["vol_spike"] = False

    # OI spikes: large absolute change_oi
    oi_mean = df["oi"].replace(0, float("nan")).mean()
    if pd.isna(oi_mean):
        oi_mean = 1.0
    df["oi_change_ratio"] = (df["change_oi"].fillna(0).abs() / (df["oi"] + 1e-9))
    oi_spike_mask = df["oi_change_ratio"] >= oi_spike_pct_threshold
    oi_spikes: List[Dict[str, Any]] = []
    for _, row in df[oi_spike_mask].iterrows():
        oi_spikes.append({
            "strike": float(row["strike"]),
            "option_type": str(row["option_type"]),
            "oi": float(row["oi"]) if pd.notna(row["oi"]) else None,
            "oi_change": float(row["change_oi"]) if pd.notna(row["change_oi"]) else None,
            "volume": float(row["volume"]) if "volume" in row and pd.notna(row.get("volume")) else None,
        })

    # Sort by abs oi_change
    oi_spikes.sort(key=lambda x: abs(x.get("oi_change") or 0), reverse=True)

    if oi_spikes:
        logger.info("[OI] Large OI spike detected at %s strikes", len(oi_spikes))

    return {
        "pcr": round(pcr, 4),
        "max_oi_call": max_oi_call,
        "max_oi_put": max_oi_put,
        "max_call_oi_strike": max_oi_call,
        "max_put_oi_strike": max_oi_put,
        "oi_spikes": oi_spikes[:20],
        "volume_spikes": volume_spikes[:20],
        "ce_oi_total": ce_oi,
        "pe_oi_total": pe_oi,
    }
