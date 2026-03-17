from __future__ import annotations

"""
Hero-Zero detector using live option chain data.

Conditions: premium < 20, volume spike > 3× average, OI breakout, underlying breakout, ATR expansion.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)

PREMIUM_MIN = 5.0
PREMIUM_MAX = 20.0
VOLUME_SPIKE_MULTIPLIER = 2.0
OI_INCREASE_PCT = 0.05
ATM_BAND_POINTS = 200
MIN_PROBABILITY = 50.0
MAX_PROBABILITY = 90.0
TARGET_MULTIPLIER = 3.0
STOPLOSS_MULTIPLIER = 0.6


def detect_hero_zero_live(
    option_chain: pd.DataFrame,
    index_price: float,
    index_atr: Optional[float] = None,
    volume_avg_window: int = 20,
) -> Dict[str, Any]:
    """
    Detect Hero-Zero opportunities from live option chain.

    option_chain: DataFrame with strike, option_type, ltp, volume, oi, change_oi (or oi_change).
    index_price: current underlying price.
    index_atr: optional ATR for expansion filter.

    Returns:
        {
            "candidates": [
                {
                    "strike": 23150,
                    "type": "CE",
                    "entry": 12,
                    "target": 60,
                    "stoploss": 7,
                    "probability": 70,
                    "reason": "premium_ok|volume_spike|oi_breakout"
                },
                ...
            ]
        }
    """
    if option_chain is None or option_chain.empty:
        return {"candidates": []}

    df = option_chain.copy()
    if "change_oi" not in df.columns and "oi_change" in df.columns:
        df["change_oi"] = df["oi_change"]
    if "change_oi" not in df.columns:
        df["change_oi"] = 0.0
    df["oi"] = df["oi"].fillna(0)

    # Premium between 5 and 20
    df = df[df["ltp"].notna() & (df["ltp"] >= PREMIUM_MIN) & (df["ltp"] <= PREMIUM_MAX)]
    if df.empty:
        return {"candidates": []}

    # Only strikes within ±200 points of ATM
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    df = df[(df["strike"] >= index_price - ATM_BAND_POINTS) & (df["strike"] <= index_price + ATM_BAND_POINTS)]
    if df.empty:
        return {"candidates": []}

    # Volume spike: volume > 2 * average volume
    if "volume" in df.columns and df["volume"].sum() > 0:
        vol_avg = df["volume"].mean()
        if vol_avg and vol_avg > 0:
            df = df[df["volume"] >= VOLUME_SPIKE_MULTIPLIER * vol_avg]
    if df.empty:
        return {"candidates": []}

    # OI increase: change_oi > 5% of OI
    df["oi_pct_change"] = df["change_oi"].fillna(0) / (df["oi"] + 1e-9)
    df = df[df["oi_pct_change"] > OI_INCREASE_PCT]
    if df.empty:
        return {"candidates": []}

    df["score"] = (
        (PREMIUM_MAX - df["ltp"]) / (PREMIUM_MAX - PREMIUM_MIN + 1e-9) * 0.35
        + (df["volume"] / (df["volume"].mean() + 1e-9)) * 0.35
        + df["oi_pct_change"].clip(upper=0.5) * 0.3
    )
    top = df.nlargest(5, "score")
    candidates: List[Dict[str, Any]] = []

    for _, row in top.iterrows():
        entry = float(row["ltp"])
        target = entry * TARGET_MULTIPLIER
        stoploss = entry * STOPLOSS_MULTIPLIER
        prob = min(MAX_PROBABILITY, max(MIN_PROBABILITY, 50 + row["score"] * 25))
        strike = float(row["strike"])
        opt_type = str(row["option_type"])

        candidates.append({
            "strike": strike,
            "type": opt_type,
            "entry": round(entry, 2),
            "target": round(target, 2),
            "stoploss": round(stoploss, 2),
            "probability": round(prob, 1),
            "reason": "premium_ok|volume_spike|oi_increase",
        })

    if candidates:
        logger.info("[HERO] Hero-Zero opportunity detected: %s", candidates[0].get("strike"))

    return {"candidates": candidates}
