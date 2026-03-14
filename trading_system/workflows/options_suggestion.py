"""
Options suggestion engine: map BUY/SELL to CALL/PUT and suggest ATM / ITM / OTM strikes.
"""

import numpy as np
import pandas as pd

from config import settings


def _round_strike(price: float, step: int = 50) -> int:
    """Round NIFTY spot to nearest strike (e.g. 50)."""
    return int(round(price / step) * step)


def suggest_strike_variants(spot: float, signal: str) -> dict:
    """
    Return ATM / ITM / OTM option strings for a BUY/SELL signal.

    BUY  -> CE side (calls)
    SELL -> PE side (puts)
    """
    if signal not in ("BUY", "SELL"):
        return {}
    step = 50
    base = _round_strike(spot, step)  # ATM

    if signal == "BUY":
        # Long CALL ideas
        return {
            "atm": f"NIFTY {base} CE",
            "itm": f"NIFTY {base - step} CE",
            "otm": f"NIFTY {base + step} CE",
        }
    else:
        # Short / bearish PUT ideas
        return {
            "atm": f"NIFTY {base} PE",
            "itm": f"NIFTY {base + step} PE",
            "otm": f"NIFTY {base - step} PE",
        }


def suggest_strike(spot: float, signal: str, confidence: float) -> str:
    """
    Backwards‑compatible single suggestion:
    - BUY  -> CE
    - SELL -> PUT
    Higher confidence biases to ATM, lower to ITM.
    """
    variants = suggest_strike_variants(spot, signal)
    if not variants:
        return ""
    # Simple rule: strong confidence -> ATM, else ITM
    if confidence is None:
        confidence = 0.0
    return variants["atm"] if confidence >= 0.70 else variants["itm"]


def add_option_suggestions(results: pd.DataFrame) -> pd.DataFrame:
    """
    Add suggested option columns:
      - suggested_option        (single primary suggestion, for backward compatibility)
      - suggested_strike        (same as suggested_option, for display)
      - suggested_option_atm
      - suggested_option_itm
      - suggested_option_otm
    """
    if results.empty:
        return results
    out = results.copy()

    primary = []
    atm_list, itm_list, otm_list = [], [], []

    for _, row in out.iterrows():
        spot = row.get("spot_price", 0) or 0
        signal = row.get("signal", "HOLD")
        conf = row.get("confidence", 0) or 0
        variants = suggest_strike_variants(spot, signal)
        primary.append(suggest_strike(spot, signal, conf))
        atm_list.append(variants.get("atm", ""))
        itm_list.append(variants.get("itm", ""))
        otm_list.append(variants.get("otm", ""))

    out["suggested_option"] = primary
    out["suggested_strike"] = out["suggested_option"]  # for display / API
    out["suggested_option_atm"] = atm_list
    out["suggested_option_itm"] = itm_list
    out["suggested_option_otm"] = otm_list
    return out
