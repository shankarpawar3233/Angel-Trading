from __future__ import annotations

from typing import Dict

import pandas as pd

from features.options_features import add_option_chain_features


def detect_institutional_flow(symbol: str, option_chain: pd.DataFrame) -> Dict:
    if option_chain.empty:
        return {"summary": None}

    feats = add_option_chain_features(option_chain)
    agg = feats["aggregates"]

    ce = agg[agg["option_type"] == "CE"].iloc[-1:] if not agg.empty else None
    pe = agg[agg["option_type"] == "PE"].iloc[-1:] if not agg.empty else None

    description = "Neutral"
    if ce is not None and not ce.empty and pe is not None and not pe.empty:
        ce_row = ce.iloc[0]
        pe_row = pe.iloc[0]
        ce_chg = ce_row["change_oi"]
        pe_chg = pe_row["change_oi"]

        if ce_chg > 0 and pe_chg < 0:
            description = "Long build-up"
        elif ce_chg < 0 and pe_chg > 0:
            description = "Short build-up"
        elif ce_chg > 0 and pe_chg > 0:
            description = "Short covering"
        elif ce_chg < 0 and pe_chg < 0:
            description = "Long unwinding"

    pcr_df = feats["pcr"]
    latest_pcr = float(pcr_df["put_call_ratio"].iloc[-1]) if not pcr_df.empty else None

    return {
        "summary": {
            "description": description,
            "put_call_ratio": latest_pcr,
        }
    }

