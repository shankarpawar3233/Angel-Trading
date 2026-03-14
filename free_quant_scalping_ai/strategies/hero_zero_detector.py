from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd

from features.options_features import detect_volume_spikes
from utils.helpers import HeroZeroSignal, round_safe


def detect_hero_zero(symbol: str, option_chain: pd.DataFrame, index_price: float) -> Dict:
    if option_chain.empty:
        return {"candidates": []}

    df = option_chain.copy()
    df = df[df["ltp"] < 20]  # cheap premiums
    if df.empty:
        return {"candidates": []}

    df = detect_volume_spikes(df)
    df = df[df["vol_spike"] == 1]
    if df.empty:
        return {"candidates": []}

    # Strong OI breakout approximation: high change_oi and volume
    df["oi_score"] = (
        (df["change_oi"].fillna(0) / (df["oi"].abs() + 1))
        + (df["volume"].fillna(0) / (df["volume"].rolling(20).mean().fillna(1)))
    )

    top = df.sort_values("oi_score", ascending=False).head(3)
    results: List[Dict] = []

    for _, row in top.iterrows():
        entry = float(row["ltp"])
        target = round_safe(entry * 4.0) or (entry * 4.0)
        stoploss = round_safe(entry * 0.6) or (entry * 0.6)

        prob = float(min(0.9, 0.5 + 0.05 * row["oi_score"]))
        expected_roi = float((target - entry) / max(entry, 1e-3))

        hz = HeroZeroSignal(
            symbol=symbol,
            strike=f"{int(row['strike'])} {row['option_type']}",
            entry=entry,
            target=target,
            stoploss=stoploss,
            probability=prob * 100.0,
            expected_roi=expected_roi * 100.0,
            generated_at=datetime.now(timezone.utc),
        )
        results.append(asdict(hz))

    return {"candidates": results}

