from __future__ import annotations

"""
Option chain reads route through angel_ws_manager (single SmartWebSocketV2).

Legacy stream removed; use start_angel_ws_manager from data.angel_ws_manager.
"""

from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def start_option_stream(
    token_list: List[Dict[str, str]],
    credentials: Optional[Dict[str, str]] = None,
) -> bool:
    """Delegate to central WebSocket manager (index + NFO options, one connection)."""
    from data.angel_ws_manager import start_angel_ws_manager

    return start_angel_ws_manager(token_list, credentials)


def get_live_option_chain_snapshot(symbol: str = "NIFTY") -> Dict[str, Any]:
    """Strike-keyed for one symbol: \"23150\" -> {\"CE\": {...}, \"PE\": {...}}."""
    from data.angel_ws_manager import get_option_chain_copy

    return get_option_chain_copy(symbol)


def get_all_live_option_chains() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """All symbols: {\"NIFTY\": {...}, \"SENSEX\": {...}}."""
    from data.angel_ws_manager import get_option_chain_copy

    return get_option_chain_copy(None)  # type: ignore[arg-type]


def get_global_option_chain_snapshot() -> Dict[str, Dict[str, Dict[str, Any]]]:
    return get_live_option_chain_snapshot()


def get_live_option_chain_as_dataframe(symbol_prefix: str = "NIFTY"):
    """Live chain as DataFrame from WS cache."""
    import pandas as pd

    snap = get_live_option_chain_snapshot()
    rows = []
    for strike, sides in snap.items():
        if not isinstance(sides, dict):
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        for opt in ("CE", "PE"):
            v = sides.get(opt) or {}
            if not v:
                continue
            rows.append(
                {
                    "symbol": symbol_prefix,
                    "strike": strike_f,
                    "option_type": opt,
                    "ltp": v.get("ltp"),
                    "volume": v.get("volume") or 0,
                    "oi": v.get("oi"),
                    "change_oi": v.get("change_oi") or v.get("oi_change"),
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def stop_option_stream() -> None:
    from data.angel_ws_manager import stop_angel_ws_manager

    stop_angel_ws_manager()
