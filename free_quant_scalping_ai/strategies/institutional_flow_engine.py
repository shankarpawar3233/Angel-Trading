from __future__ import annotations

"""
Institutional flow engine: classify Long buildup, Short buildup, Short covering, Long unwinding.

Logic:
  Long buildup:   price ↑, OI ↑
  Short buildup:  price ↓, OI ↑
  Short covering: price ↑, OI ↓
  Long unwinding: price ↓, OI ↓
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from utils.logger import get_logger


logger = get_logger(__name__)


def detect_institutional_flow_from_chain(
    option_chain: pd.DataFrame,
    prev_chain: Optional[pd.DataFrame] = None,
    min_oi_change_pct: float = 0.02,
) -> Dict[str, Any]:
    """
    Detect institutional flow from option chain.

    If prev_chain is provided, compare current vs previous to get price and OI deltas per strike.
    If not, use change_oi and ltp to infer (single snapshot: flow from aggregates).

    Returns:
        {
            "flow_type": "LONG_BUILDUP" | "SHORT_BUILDUP" | "SHORT_COVERING" | "LONG_UNWINDING" | "NEUTRAL",
            "strike": float (most significant strike if per-strike),
            "confidence": int 0-100,
            "details": [{"strike", "option_type", "flow_type", "confidence"}, ...],
            "summary": "Long build-up" etc for backward compat
        }
    """
    if option_chain is None or option_chain.empty:
        return {
            "flow_type": "NEUTRAL",
            "strike": None,
            "confidence": 0,
            "details": [],
            "summary": {"description": "Neutral", "put_call_ratio": None},
        }

    df = option_chain.copy()
    if "change_oi" not in df.columns and "oi_change" in df.columns:
        df["change_oi"] = df["oi_change"]
    if "change_oi" not in df.columns:
        df["change_oi"] = 0.0

    # Aggregate CE vs PE OI change for summary
    ce_chg = df.loc[df["option_type"] == "CE", "change_oi"].sum()
    pe_chg = df.loc[df["option_type"] == "PE", "change_oi"].sum()
    ce_chg = 0.0 if ce_chg is None or (isinstance(ce_chg, float) and pd.isna(ce_chg)) else float(ce_chg)
    pe_chg = 0.0 if pe_chg is None or (isinstance(pe_chg, float) and pd.isna(pe_chg)) else float(pe_chg)

    # PCR
    ce_oi = df.loc[df["option_type"] == "CE", "oi"].sum()
    pe_oi = df.loc[df["option_type"] == "PE", "oi"].sum()
    ce_oi = float(ce_oi) if ce_oi is not None else 0.0
    pe_oi = float(pe_oi) if pe_oi is not None else 0.0
    pcr = pe_oi / (ce_oi + 1e-9)

    description = "Neutral"
    flow_type = "NEUTRAL"
    if ce_chg > 0 and pe_chg < 0:
        description = "Long build-up"
        flow_type = "LONG_BUILDUP"
    elif ce_chg < 0 and pe_chg > 0:
        description = "Short build-up"
        flow_type = "SHORT_BUILDUP"
    elif ce_chg > 0 and pe_chg > 0:
        description = "Short covering"
        flow_type = "SHORT_COVERING"
    elif ce_chg < 0 and pe_chg < 0:
        description = "Long unwinding"
        flow_type = "LONG_UNWINDING"

    # Confidence from magnitude of OI change
    total_oi = ce_oi + pe_oi
    abs_chg = abs(ce_chg) + abs(pe_chg)
    chg_pct = (abs_chg / (total_oi + 1e-9)) * 100
    confidence = min(95, int(50 + min(45, chg_pct * 2)))

    # Per-strike flow (if we have prev: price delta + OI delta)
    details: List[Dict[str, Any]] = []
    if prev_chain is not None and not prev_chain.empty and "strike" in prev_chain.columns and "option_type" in prev_chain.columns:
        prev = prev_chain.copy()
        if "change_oi" not in prev.columns:
            prev["change_oi"] = 0.0
        for _, row in df.iterrows():
            sk, ot = row["strike"], row["option_type"]
            prev_row = prev[(prev["strike"] == sk) & (prev["option_type"] == ot)]
            if prev_row.empty:
                continue
            pr = prev_row.iloc[0]
            price_delta = (row.get("ltp") or 0) - (pr.get("ltp") or 0)
            oi_delta = (row.get("change_oi") or row.get("oi_change") or 0)
            if abs(oi_delta) < min_oi_change_pct * (row.get("oi") or 1):
                continue
            if price_delta > 0 and oi_delta > 0:
                st_flow = "LONG_BUILDUP"
            elif price_delta < 0 and oi_delta > 0:
                st_flow = "SHORT_BUILDUP"
            elif price_delta > 0 and oi_delta < 0:
                st_flow = "SHORT_COVERING"
            elif price_delta < 0 and oi_delta < 0:
                st_flow = "LONG_UNWINDING"
            else:
                st_flow = "NEUTRAL"
            conf = min(90, int(60 + abs(oi_delta) / 1000))
            details.append({"strike": sk, "option_type": ot, "flow_type": st_flow, "confidence": conf})

    if flow_type != "NEUTRAL":
        logger.info("[FLOW] Institutional %s", flow_type)

    return {
        "flow_type": flow_type,
        "strike": df["strike"].iloc[0] if not df.empty else None,
        "confidence": confidence,
        "details": details,
        "summary": {
            "description": description,
            "put_call_ratio": round(pcr, 4),
        },
    }
