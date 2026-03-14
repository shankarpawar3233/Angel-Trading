from __future__ import annotations

import pandas as pd


def add_option_chain_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    # Put-Call Ratio by expiry
    pcr = (
        df.pivot_table(
            index=["symbol", "ts", "expiry"],
            columns="option_type",
            values="oi",
            aggfunc="sum",
        )
        .fillna(0.0)
        .reset_index()
    )
    pcr["put_call_ratio"] = pcr.get("PE", 0.0) / (pcr.get("CE", 0.0) + 1e-6)

    # OI change aggregates to detect institutional flow
    agg = df.groupby(["symbol", "ts", "option_type"], as_index=False).agg(
        {
            "oi": "sum",
            "change_oi": "sum",
            "volume": "sum",
        }
    )

    return {
        "raw": df,
        "pcr": pcr,
        "aggregates": agg,
    }


def detect_volume_spikes(df: pd.DataFrame, threshold: float = 3.0) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = (df["volume"] > threshold * df["vol_ma"]).astype(int)
    return df

