"""
Stop-hunt detector.

Detects liquidity grabs: price breaks resistance/support with volume spike,
large OI cluster nearby, then quick reversal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def detect_stop_hunt(
    candles: pd.DataFrame,
    option_chain: pd.DataFrame,
    liquidity_map: Dict[str, Any],
    volume_spike_multiplier: float = 2.0,
    reversal_bars: int = 3,
    min_oi_cluster_strikes: int = 2,
) -> Dict[str, Any]:
    """
    Detect stop-hunt zones (liquidity grabs).

    Conditions:
      - Price breaks resistance or support
      - Volume spike
      - Large OI cluster nearby (from liquidity_map)
      - Quick reversal within reversal_bars

    Returns:
        {
            "stop_hunt_zone": {"type": "RESISTANCE"|"SUPPORT", "level": float, "reversal_price": float} or None,
            "detected": bool,
            "details": [...],
        }
    """
    if candles is None or candles.empty or len(candles) < reversal_bars + 5:
        return {"stop_hunt_zone": None, "detected": False, "details": []}

    df = candles.copy()
    df = df.sort_values("ts" if "ts" in df.columns else df.index.name or "index").reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values if "volume" in df.columns and "volume" in df else None
    if volume is not None and len(volume) > 0:
        vol_avg = float(np.mean(volume))
    else:
        vol_avg = 1.0
    if vol_avg <= 0:
        vol_avg = 1.0

    resistance_zones = (liquidity_map or {}).get("resistance_zones", [])
    support_zones = (liquidity_map or {}).get("support_zones", [])
    clusters = (liquidity_map or {}).get("stop_loss_clusters", [])
    has_oi_cluster = len(clusters) >= min_oi_cluster_strikes

    details: List[Dict[str, Any]] = []
    stop_hunt_zone = None

    # Recent resistance break then reversal
    for i in range(n - reversal_bars - 2, max(0, n - 50), -1):
        if i < 2:
            break
        res_level = None
        for z in resistance_zones:
            level = z.get("strike")
            if level is None:
                continue
            if high[i] >= level and close[i] > close[i - 1]:
                res_level = level
                break
        if res_level is None:
            continue
        vol_spike = volume is not None and volume[i] >= volume_spike_multiplier * vol_avg
        reversal = all(close[i + k] < close[i] for k in range(1, min(reversal_bars + 1, n - i)))
        if vol_spike and reversal and has_oi_cluster:
            stop_hunt_zone = {
                "type": "RESISTANCE",
                "level": float(res_level),
                "reversal_price": float(close[min(i + reversal_bars, n - 1)]),
            }
            details.append({"index": i, "type": "RESISTANCE", "level": res_level})
            logger.info("[STOPHUNT] Liquidity grab detected: resistance %s", res_level)
            break

    if stop_hunt_zone is None:
        for i in range(n - reversal_bars - 2, max(0, n - 50), -1):
            if i < 2:
                break
            sup_level = None
            for z in support_zones:
                level = z.get("strike")
                if level is None:
                    continue
                if low[i] <= level and close[i] < close[i - 1]:
                    sup_level = level
                    break
            if sup_level is None:
                continue
            vol_spike = volume is not None and volume[i] >= volume_spike_multiplier * vol_avg
            reversal = all(close[i + k] > close[i] for k in range(1, min(reversal_bars + 1, n - i)))
            if vol_spike and reversal and has_oi_cluster:
                stop_hunt_zone = {
                    "type": "SUPPORT",
                    "level": float(sup_level),
                    "reversal_price": float(close[min(i + reversal_bars, n - 1)]),
                }
                details.append({"index": i, "type": "SUPPORT", "level": sup_level})
                logger.info("[STOPHUNT] Liquidity grab detected: support %s", sup_level)
                break

    return {
        "stop_hunt_zone": stop_hunt_zone,
        "detected": stop_hunt_zone is not None,
        "details": details,
    }
