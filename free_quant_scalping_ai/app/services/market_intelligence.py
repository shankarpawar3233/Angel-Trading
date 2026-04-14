from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def _safe_float(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def build_market_intelligence(
    symbol: str,
    price_history: List[float],
    market_status: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Derive lightweight market intelligence for strategy selection.
    Falls back to NORMAL defaults when data is insufficient.
    """
    hist = [float(x) for x in (price_history or []) if _safe_float(x, 0.0) > 0]
    status = market_status or {}
    su = str(symbol or "").upper()
    if len(hist) < 24:
        return {
            "symbol": su,
            "market_type": "RANGING",
            "volatility_level": "LOW",
            "trend_direction": "SIDEWAYS",
            "is_expiry": bool(status.get("is_expiry")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    price = hist[-1]
    win20 = hist[-20:]
    short = hist[-10:]
    long = hist[-20:]
    short_ma = sum(short) / len(short)
    long_ma = sum(long) / len(long)
    trend_delta = short_ma - long_ma
    trend_frac = abs(trend_delta) / max(1.0, price)
    vol_frac = (max(win20) - min(win20)) / max(1.0, price)

    if trend_delta > 0:
        trend_direction = "UP"
    elif trend_delta < 0:
        trend_direction = "DOWN"
    else:
        trend_direction = "SIDEWAYS"

    if vol_frac >= 0.006:
        market_type = "VOLATILE"
    elif trend_frac >= 0.00035:
        market_type = "TRENDING"
    else:
        market_type = "RANGING"

    if vol_frac >= 0.006:
        vol_level = "HIGH"
    elif vol_frac >= 0.003:
        vol_level = "MEDIUM"
    else:
        vol_level = "LOW"

    return {
        "symbol": su,
        "market_type": market_type,
        "volatility_level": vol_level,
        "trend_direction": trend_direction,
        "is_expiry": bool(status.get("is_expiry")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "vol_frac": round(vol_frac, 6),
            "trend_frac": round(trend_frac, 6),
            "short_ma": round(short_ma, 3),
            "long_ma": round(long_ma, 3),
        },
    }
