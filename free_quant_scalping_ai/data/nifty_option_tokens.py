"""
Index option token discovery from Angel OpenAPIScripMaster.json.

Supports:
- NIFTY (NFO, exchangeType=2)
- SENSEX (BFO, exchangeType=4)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from data.angel_instruments import load_instruments
from utils.logger import get_logger

logger = get_logger(__name__)


def _strike_from_row(inst: Dict[str, Any]) -> Optional[float]:
    raw = inst.get("strike")
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # Angel typically stores e.g. 2370000 → 23700; if already ~23700, use as-is
    s = v / 100.0
    if 5000 <= s <= 100000:
        return round(s, 2)
    if 5000 <= v <= 100000:
        return float(v)
    if v > 100000:
        return round(v / 100.0, 2)
    return None


def _get_index_option_tokens(
    instruments: List[Dict[str, Any]],
    *,
    index_name: str,
    exch_seg: str,
    exchange_type: int,
    spot_price: float,
    atm_range: float,
    max_tokens: int,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    spot = float(spot_price)
    name_u = index_name.upper()
    token_map: Dict[str, Dict[str, Any]] = {}

    for inst in instruments:
        exch = str(inst.get("exch_seg") or "").upper()
        if exch != exch_seg:
            continue
        if str(inst.get("instrumenttype") or "").upper() != "OPTIDX":
            continue
        sym = str(inst.get("symbol") or "").upper()
        nm = str(inst.get("name") or "").upper()
        if not sym.endswith(("CE", "PE")):
            continue
        if name_u not in nm and not sym.startswith(name_u):
            continue
        strike = _strike_from_row(inst)
        if strike is None or abs(strike - spot) > float(atm_range):
            continue
        tok = str(inst.get("token") or "").strip()
        if not tok.isdigit():
            continue
        token_map[tok] = {
            "symbol": name_u,
            "strike": strike,
            "type": "CE" if sym.endswith("CE") else "PE",
            "exchangeType": int(exchange_type),
        }

    items = sorted(token_map.items(), key=lambda kv: (abs(float(kv[1]["strike"]) - spot), kv[0]))[: int(max_tokens)]
    token_map = dict(items)
    tokens = list(token_map.keys())
    logger.info("[%s TOKENS] count=%s spot=%s atm±%s", name_u, len(tokens), spot, atm_range)
    return tokens, token_map


def get_nifty_option_tokens(
    instruments: List[Dict[str, Any]],
    spot_price: float,
    atm_range: float = 600.0,
    max_tokens: int = 240,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    out = _get_index_option_tokens(
        instruments,
        index_name="NIFTY",
        exch_seg="NFO",
        exchange_type=2,
        spot_price=spot_price,
        atm_range=atm_range,
        max_tokens=max_tokens,
    )
    print("[NFO TOKENS]", len(out[0]))
    return out


def get_sensex_option_tokens(
    instruments: List[Dict[str, Any]],
    spot_price: float,
    atm_range: float = 1200.0,
    max_tokens: int = 200,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    out = _get_index_option_tokens(
        instruments,
        index_name="SENSEX",
        exch_seg="BFO",
        exchange_type=4,
        spot_price=spot_price,
        atm_range=atm_range,
        max_tokens=max_tokens,
    )
    print("[BFO TOKENS]", len(out[0]))
    return out


def refresh_master() -> List[Dict[str, Any]]:
    """Force reload from URL/cache (see angel_instruments)."""
    from data import angel_instruments as ai

    ai._INSTRUMENT_CACHE = None
    return load_instruments()
