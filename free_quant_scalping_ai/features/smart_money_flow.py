"""
Smart money footprint: detect institutional accumulation from price + OI + volume patterns.

Price ↑ + OI ↑ + Volume ↑ → smart money buying calls (CALL_ACCUMULATION)
Price ↓ + OI ↑ + Volume ↑ → smart money buying puts (PUT_ACCUMULATION)
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def compute_smart_money_flow(
    option_chain: pd.DataFrame,
    price_change: float,
    prev_oi_total: float | None = None,
    prev_volume_total: float | None = None,
) -> Dict[str, Any]:
    """
    Use current option chain and price change (e.g. from last candle or last N candles).
    If prev_oi_total/prev_volume_total not provided, use chain aggregates and assume accumulation
    when price_change aligns with OI/volume build. Patterns:
    Price ↑ + OI ↑ + Volume ↑ → CALL_ACCUMULATION
    Price ↓ + OI ↑ + Volume ↑ → PUT_ACCUMULATION
    Returns smart_money_direction, strength 0-100.
    """
    if option_chain is None or option_chain.empty:
        return {"smart_money_direction": "NONE", "strength": 0}

    df = option_chain.copy()
    df["oi"] = df["oi"].fillna(0)
    df["volume"] = df["volume"].fillna(0)
    if "change_oi" not in df.columns and "oi_change" in df.columns:
        df["change_oi"] = df["oi_change"]
    oi_chg = float(df["change_oi"].sum()) if "change_oi" in df.columns else 0.0
    vol_total = float(df["volume"].sum())
    oi_total = float(df["oi"].sum())

    if price_change > 0 and oi_chg > 0 and vol_total > 0:
        smart_money_direction = "CALL_ACCUMULATION"
        strength = min(100, int(50 + min(30, abs(price_change) * 100) + min(20, oi_chg / (oi_total + 1e-9) * 1000)))
    elif price_change < 0 and oi_chg > 0 and vol_total > 0:
        smart_money_direction = "PUT_ACCUMULATION"
        strength = min(100, int(50 + min(30, abs(price_change) * 100) + min(20, oi_chg / (oi_total + 1e-9) * 1000)))
    else:
        smart_money_direction = "NONE"
        strength = 0

    if smart_money_direction != "NONE":
        logger.info("[FLOW] smart money footprint: %s strength=%s", smart_money_direction, strength)

    return {"smart_money_direction": smart_money_direction, "strength": strength}
