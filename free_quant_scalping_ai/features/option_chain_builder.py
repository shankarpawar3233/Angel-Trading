"""
Build structured option chain from Angel WebSocket live_option_chain.

Converts flat dict "23150CE" -> {ltp, volume, oi, oi_change} into strike-keyed:
  option_chain["23150"] = {"CE": {...}, "PE": {...}}
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def build_option_chain(live_option_chain: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Convert live_option_chain (key = "23150CE" / "23150PE") into strike-keyed structure.

    Returns:
        {
            "23150": {"CE": {"ltp": 21.5, "volume": 120000, "oi": 450000, "oi_change": 5000},
                      "PE": {"ltp": 18.2, ...}},
            ...
        }
    """
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for key, data in (live_option_chain or {}).items():
        if not isinstance(key, str) or not key:
            continue
        key = key.strip().upper()
        if key.endswith("CE"):
            strike_str = key[:-2]
            opt_type = "CE"
        elif key.endswith("PE"):
            strike_str = key[:-2]
            opt_type = "PE"
        else:
            continue
        try:
            _ = float(strike_str)
        except ValueError:
            continue
        if strike_str not in result:
            result[strike_str] = {}
        result[strike_str][opt_type] = {
            "ltp": data.get("ltp"),
            "volume": data.get("volume", 0),
            "oi": data.get("oi"),
            "oi_change": data.get("oi_change"),
            "symbol": data.get("symbol"),
            "token": data.get("token"),
        }
    return result


def option_chain_to_dataframe(
    live_option_chain: Dict[str, Any],
    symbol_prefix: str = "NIFTY",
) -> pd.DataFrame:
    """
    Convert option chain snapshot to a DataFrame with columns strike, option_type, ltp, volume, oi, change_oi.
    Accepts either:
    - Flat format: key = "23150CE" / "23150PE", value = {ltp, volume, oi, oi_change}
    - Strike-keyed: key = "23150", value = {"CE": {ltp, volume, oi, change_oi}, "PE": {...}}
    """
    rows: list = []
    raw = live_option_chain or {}

    # Strike-keyed format: "23150" -> {"CE": {...}, "PE": {...}}
    def is_strike_keyed():
        if not raw:
            return False
        first_key = next(iter(raw))
        first_val = raw[first_key]
        return (
            isinstance(first_val, dict)
            and not (first_key.endswith("CE") or first_key.endswith("PE"))
            and ("CE" in first_val or "PE" in first_val)
        )

    if is_strike_keyed():
        for strike_str, strike_data in raw.items():
            if not isinstance(strike_data, dict):
                continue
            try:
                strike = float(strike_str)
            except (ValueError, TypeError):
                continue
            for opt_type in ("CE", "PE"):
                data = strike_data.get(opt_type)
                if not isinstance(data, dict):
                    continue
                rows.append({
                    "symbol": symbol_prefix,
                    "strike": strike,
                    "option_type": opt_type,
                    "ltp": data.get("ltp"),
                    "volume": data.get("volume") or 0,
                    "oi": data.get("oi"),
                    "change_oi": data.get("change_oi") or data.get("oi_change"),
                })
    else:
        # Flat format: "23150CE" -> {ltp, volume, oi, oi_change}
        for key, data in raw.items():
            if not isinstance(key, str) or not isinstance(data, dict):
                continue
            key = key.strip().upper()
            if key.endswith("CE"):
                strike_str = key[:-2]
                opt_type = "CE"
            elif key.endswith("PE"):
                strike_str = key[:-2]
                opt_type = "PE"
            else:
                continue
            try:
                strike = float(strike_str)
            except ValueError:
                continue
            rows.append({
                "symbol": symbol_prefix,
                "strike": strike,
                "option_type": opt_type,
                "ltp": data.get("ltp"),
                "volume": data.get("volume") or 0,
                "oi": data.get("oi"),
                "change_oi": data.get("change_oi") or data.get("oi_change"),
            })

    if not rows:
        try:
            print("[CHAIN DF rows] 0")
        except Exception:
            logger.debug("[CHAIN DF rows] 0")
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    try:
        print("[CHAIN DF rows]", len(df))
    except Exception:
        logger.debug("[CHAIN DF rows] %s", len(df))
    return df
