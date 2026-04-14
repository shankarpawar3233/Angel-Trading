from __future__ import annotations

"""
Automatically discover NIFTY weekly option contracts from the Angel instrument master.

Filters: symbol contains "NIFTY", exch_seg == "NFO", instrumenttype == "OPTIDX".
Returns list of tokens for WebSocket subscription and ATM ±20 strikes.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from data.angel_instruments import load_instruments
from utils.logger import get_logger


logger = get_logger(__name__)

# NFO exchange type for Angel WebSocket (2 = NFO)
NFO_EXCHANGE_TYPE = 2


def discover_nifty_options(
    index_price: Optional[float] = None,
    atm_band: int = 20,
) -> List[Dict[str, Any]]:
    """
    Discover NIFTY option contracts from Angel instrument master.

    Filter: exch_seg == "NFO", name == "NIFTY" (or symbol contains "NIFTY"),
    instrumenttype == "OPTIDX".

    Returns list of {"symbol", "token", "strike", "expiry", "option_type"}.
    If index_price is set, restricts to ATM ± atm_band strikes (CE and PE).
    """
    instruments = load_instruments()
    option_contracts: List[Dict[str, Any]] = []

    for inst in instruments:
        exch = str(inst.get("exch_seg", "")).upper()
        if exch != "NFO":
            continue
        itype = str(inst.get("instrumenttype", "")).upper()
        if itype != "OPTIDX":
            continue
        name = str(inst.get("name", "")).strip().upper()
        sym = str(inst.get("symbol", "")).upper()
        if name != "NIFTY":
            continue

        parsed = _parse_option_symbol(sym)
        if not parsed:
            continue
        strike, option_type = parsed
        token = inst.get("token")
        if not token:
            continue
        expiry = _parse_expiry_from_symbol(sym)
        option_contracts.append({
            "symbol": inst.get("symbol", sym),
            "token": str(token),
            "strike": strike,
            "expiry": expiry,
            "option_type": option_type,
        })

    option_contracts.sort(key=lambda x: (x["strike"], x["option_type"]))

    if index_price is not None and option_contracts:
        strikes_seen = sorted({c["strike"] for c in option_contracts})
        if strikes_seen:
            step = 50.0 if strikes_seen[0] > 1000 else 100.0
            if len(strikes_seen) > 1:
                step = min(abs(strikes_seen[i + 1] - strikes_seen[i]) for i in range(len(strikes_seen) - 1))
            atm = round(index_price / step) * step
            low = atm - atm_band * step
            high = atm + atm_band * step
            option_contracts = [c for c in option_contracts if low <= c["strike"] <= high]

    logger.info("[OPTIONS] discover_nifty_options: %s contracts (ATM±%s)", len(option_contracts), atm_band)
    return option_contracts


def _parse_option_symbol(symbol: str) -> Optional[Tuple[float, str]]:
    """
    Parse NIFTY option symbol to extract strike and option_type (CE/PE).
    e.g. NIFTY24MAR23150CE -> (23150.0, "CE"), NIFTY27Mar2423150PE -> (23150.0, "PE")
    """
    symbol = (symbol or "").strip().upper()
    if not symbol or "NIFTY" not in symbol:
        return None
    if symbol.endswith("CE"):
        option_type = "CE"
        base = symbol[:-2]
    elif symbol.endswith("PE"):
        option_type = "PE"
        base = symbol[:-2]
    else:
        return None
    # Strike is typically the last numeric part (e.g. 23150)
    numbers = re.findall(r"\d+", base)
    if not numbers:
        return None
    try:
        strike = float(numbers[-1])
        return (strike, option_type)
    except (ValueError, IndexError):
        return None


def _parse_expiry_from_symbol(symbol: str) -> Optional[str]:
    """Extract expiry string from symbol if present (e.g. 24MAR, 27Mar24)."""
    symbol = (symbol or "").strip().upper()
    # Match patterns like 24MAR, 27MAR24, 24MAR2024
    m = re.search(r"NIFTY(\d{2}[A-Z]{3}\d{0,4})", symbol)
    if m:
        return m.group(1)
    return None


def discover_nifty_option_tokens(
    index_price: Optional[float] = None,
    atm_band: int = 20,
    max_contracts: int = 500,
) -> List[Dict[str, Any]]:
    """
    Discover NIFTY weekly option contracts from Angel instrument master.

    Filter: symbol contains "NIFTY", exch_seg == "NFO", instrumenttype == "OPTIDX"
    (or CE/PE for masters that use option_type as instrumenttype).

    If index_price is given, computes ATM and returns ±atm_band strikes.
    Otherwise returns all discovered contracts up to max_contracts.

    Returns list of {"symbol": "NIFTY24MAR23150CE", "token": "12345", "strike", "expiry", "option_type"}.
    """
    instruments = load_instruments()
    symbol_u = "NIFTY"
    option_contracts: List[Dict[str, Any]] = []

    for inst in instruments:
        sym = str(inst.get("symbol", "")).upper()
        exch = str(inst.get("exch_seg", "")).upper()
        itype = str(inst.get("instrumenttype", "")).upper()

        if symbol_u not in sym or exch != "NFO":
            continue
        # OPTIDX = index options; some masters use CE/PE as instrumenttype
        if itype not in ("OPTIDX", "CE", "PE") and not (sym.endswith("CE") or sym.endswith("PE")):
            continue

        parsed = _parse_option_symbol(sym)
        if not parsed:
            continue
        strike, option_type = parsed
        token = inst.get("token")
        if not token:
            continue
        expiry = _parse_expiry_from_symbol(sym)

        option_contracts.append({
            "symbol": inst.get("symbol", sym),
            "token": str(token),
            "strike": strike,
            "expiry": expiry,
            "option_type": option_type,
        })

    # Sort by strike for consistent ATM band
    option_contracts.sort(key=lambda x: (x["strike"], x["option_type"]))

    if index_price is not None and option_contracts:
        # Round to nearest strike (NIFTY strikes are usually 50 apart)
        strikes_seen = sorted({c["strike"] for c in option_contracts})
        if strikes_seen:
            step = 50.0 if strikes_seen[0] > 1000 else 100.0
            if len(strikes_seen) > 1:
                step = min(abs(strikes_seen[i + 1] - strikes_seen[i]) for i in range(len(strikes_seen) - 1))
            atm = round(index_price / step) * step
            low = atm - atm_band * step
            high = atm + atm_band * step
            option_contracts = [c for c in option_contracts if low <= c["strike"] <= high]

    out = option_contracts[:max_contracts]
    logger.info("[OPTIONS] Discovered %s NIFTY option contracts (ATM band applied: %s)", len(out), index_price is not None)
    return out


def get_subscription_tokens(
    index_price: Optional[float] = None,
    atm_band: int = 20,
    max_tokens: int = 500,
) -> List[Dict[str, str]]:
    """
    Return a list of {symbol, token} for WebSocket subscription.
    Uses discover_nifty_options (NFO, name NIFTY, OPTIDX) then ATM ± atm_band strikes.

    Contracts are ordered by distance to spot (nearest ATM first). Previously they were
    sorted by strike ascending, so [:max_tokens] picked the *lowest* strikes in the band
    (far below spot) — those rarely tick; the chain stayed empty.
    """
    contracts = discover_nifty_options(index_price=index_price, atm_band=atm_band)
    if not contracts:
        contracts = discover_nifty_option_tokens(
            index_price=index_price,
            atm_band=atm_band,
            max_contracts=max(500, max_tokens * 4),
        )
    ref = float(index_price) if index_price is not None else None
    if ref is not None and contracts:

        def _key(c: Dict[str, Any]) -> Tuple[float, str]:
            return (abs(float(c["strike"]) - ref), str(c.get("symbol", "")))

        contracts = sorted(contracts, key=_key)
    elif contracts:
        contracts = sorted(contracts, key=lambda c: float(c["strike"]))
    out = [{"symbol": c["symbol"], "token": c["token"]} for c in contracts[:max_tokens]]
    return out
