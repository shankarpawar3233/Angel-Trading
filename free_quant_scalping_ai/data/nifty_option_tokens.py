"""
Index option token discovery from Angel OpenAPIScripMaster.json.

Supports:
- NIFTY (NFO, exchangeType=2)
- SENSEX (BFO, exchangeType=4)
"""

from __future__ import annotations

from datetime import datetime
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
    expiry_count: int = 2,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    spot = float(spot_price)
    name_u = index_name.upper()
    token_map: Dict[str, Dict[str, Any]] = {}

    filtered: List[Dict[str, Any]] = []
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
        filtered.append(inst)

    # Prefer nearest expiries to keep subscriptions liquid and moving.
    expiries_raw = [str(x.get("expiry") or "").strip().upper() for x in filtered if str(x.get("expiry") or "").strip()]
    expiry_dates: List[Tuple[datetime, str]] = []
    for e in set(expiries_raw):
        try:
            expiry_dates.append((datetime.strptime(e, "%d%b%Y"), e))
        except ValueError:
            continue
    expiry_dates.sort(key=lambda x: x[0])
    exp_n = max(1, int(expiry_count))
    selected_expiries = {x[1] for x in expiry_dates[:exp_n]} if expiry_dates else set()

    for inst in filtered:
        exp = str(inst.get("expiry") or "").strip().upper()
        if selected_expiries and exp and exp not in selected_expiries:
            continue
        sym = str(inst.get("symbol") or "").upper()
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

    # Balanced selection: prefer nearest strikes with both CE/PE coverage.
    strike_groups: Dict[float, Dict[str, List[Tuple[str, Dict[str, Any]]]]] = {}
    for tok, meta in token_map.items():
        s = float(meta["strike"])
        t = str(meta.get("type") or "")
        strike_groups.setdefault(s, {"CE": [], "PE": []})
        if t in ("CE", "PE"):
            strike_groups[s][t].append((tok, meta))
    ordered_strikes = sorted(strike_groups.keys(), key=lambda s: abs(s - spot))
    balanced_items: List[Tuple[str, Dict[str, Any]]] = []
    for s in ordered_strikes:
        ce_list = sorted(strike_groups[s]["CE"], key=lambda x: x[0])
        pe_list = sorted(strike_groups[s]["PE"], key=lambda x: x[0])
        if ce_list:
            balanced_items.append(ce_list[0])
        if pe_list:
            balanced_items.append(pe_list[0])
        if len(balanced_items) >= int(max_tokens):
            break
    if len(balanced_items) < int(max_tokens):
        used = {t for t, _ in balanced_items}
        extras = sorted(
            [(t, m) for t, m in token_map.items() if t not in used],
            key=lambda kv: (abs(float(kv[1]["strike"]) - spot), kv[0]),
        )
        need = int(max_tokens) - len(balanced_items)
        balanced_items.extend(extras[:need])
    token_map = dict(balanced_items[: int(max_tokens)])
    tokens = list(token_map.keys())
    ce_count = sum(1 for _t, m in token_map.items() if str(m.get("type") or "") == "CE")
    pe_count = sum(1 for _t, m in token_map.items() if str(m.get("type") or "") == "PE")
    logger.info(
        "[%s TOKENS] count=%s CE=%s PE=%s spot=%s atm±%s expiries=%s",
        name_u,
        len(tokens),
        ce_count,
        pe_count,
        spot,
        atm_range,
        sorted(selected_expiries) if selected_expiries else ["ALL"],
    )
    return tokens, token_map


def get_nifty_option_tokens(
    instruments: List[Dict[str, Any]],
    spot_price: float,
    atm_range: float = 600.0,
    max_tokens: int = 240,
    expiry_count: int = 2,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    out = _get_index_option_tokens(
        instruments,
        index_name="NIFTY",
        exch_seg="NFO",
        exchange_type=2,
        spot_price=spot_price,
        atm_range=atm_range,
        max_tokens=max_tokens,
        expiry_count=expiry_count,
    )
    print("[NFO TOKENS]", len(out[0]))
    return out


def get_sensex_option_tokens(
    instruments: List[Dict[str, Any]],
    spot_price: float,
    atm_range: float = 1200.0,
    max_tokens: int = 200,
    expiry_count: int = 1,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    out = _get_index_option_tokens(
        instruments,
        index_name="SENSEX",
        exch_seg="BFO",
        exchange_type=4,
        spot_price=spot_price,
        atm_range=atm_range,
        max_tokens=max_tokens,
        expiry_count=expiry_count,
    )
    print("[BFO TOKENS]", len(out[0]))
    return out


def refresh_master() -> List[Dict[str, Any]]:
    """Force reload from URL/cache (see angel_instruments)."""
    from data import angel_instruments as ai

    ai._INSTRUMENT_CACHE = None
    return load_instruments()
