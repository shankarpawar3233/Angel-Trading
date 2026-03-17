"""
Liquidity trap detection: fake breakouts where stop-loss liquidity gets hunted.

Detect rapid move beyond support/resistance, volume spike, OI decrease, then quick return → trap.
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def detect_liquidity_trap(
    candles: pd.DataFrame,
    option_chain: pd.DataFrame,
    liquidity_map: Dict[str, Any],
    lookback_bars: int = 10,
    return_bars: int = 3,
    volume_spike_mult: float = 1.5,
) -> Dict[str, Any]:
    """
    Detect rapid move beyond S/R cluster, volume spike, OI decrease, then price returns quickly → trap.
    Returns trap_detected (bool), trap_type (BULL_TRAP | BEAR_TRAP), confidence 0-100.
    """
    if candles is None or candles.empty or len(candles) < lookback_bars + return_bars:
        return {"trap_detected": False, "trap_type": None, "confidence": 0}

    df = candles.copy()
    if "ts" in df.columns:
        df = df.sort_values("ts").reset_index(drop=True)
    df = df.tail(lookback_bars + return_bars)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values if "volume" in df.columns else None
    vol_avg = float(volume.mean()) if volume is not None and len(volume) > 5 else 1.0
    if vol_avg <= 0:
        vol_avg = 1.0

    res_zones = (liquidity_map or {}).get("resistance_zones", [])
    sup_zones = (liquidity_map or {}).get("support_zones", [])
    res_levels = [z.get("strike") for z in res_zones if z.get("strike") is not None]
    sup_levels = [z.get("strike") for z in sup_zones if z.get("strike") is not None]

    trap_detected = False
    trap_type = None
    confidence = 0

    for i in range(n - return_bars - 2, max(0, n - 20), -1):
        if i < 2:
            break
        vol_spike = volume is not None and volume[i] >= volume_spike_mult * vol_avg
        if not vol_spike:
            continue
        for level in res_levels:
            if high[i] >= level and close[i] < level:
                ret_back = all(close[i + k] < level for k in range(1, min(return_bars + 1, n - i)))
                if ret_back:
                    trap_detected = True
                    trap_type = "BEAR_TRAP"
                    confidence = min(90, 50 + int(volume[i] / vol_avg * 10))
                    break
        if trap_detected:
            break
        for level in sup_levels:
            if low[i] <= level and close[i] > level:
                ret_back = all(close[i + k] > level for k in range(1, min(return_bars + 1, n - i)))
                if ret_back:
                    trap_detected = True
                    trap_type = "BULL_TRAP"
                    confidence = min(90, 50 + int(volume[i] / vol_avg * 10))
                    break
        if trap_detected:
            break

    if option_chain is not None and not option_chain.empty and "change_oi" in option_chain.columns:
        oi_chg = option_chain["change_oi"].sum()
        if trap_detected and oi_chg < 0:
            confidence = min(95, confidence + 10)

    if trap_detected:
        logger.info("[TRAP] liquidity trap: %s confidence=%s", trap_type, confidence)

    return {"trap_detected": trap_detected, "trap_type": trap_type, "confidence": confidence}
