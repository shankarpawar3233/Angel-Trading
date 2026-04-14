"""
Index option token discovery from Angel OpenAPIScripMaster.json.

Supports:
- NIFTY (NFO, exchangeType=2)
- SENSEX (BFO, exchangeType=4)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = timezone(timedelta(hours=5, minutes=30))

from data.angel_instruments import INSTRUMENTS_PATH, load_instruments
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
    expiry_count: int = 1,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    spot = float(spot_price)
    name_u = index_name.upper()
    # Master row ``name`` must match the index exactly. Substring checks like "NIFTY" in "FINNIFTY"
    # pulled the wrong product: same strike (e.g. 23550 CE) but totally different premiums.
    allowed_inst_names = {
        "NIFTY": {"NIFTY"},
        "SENSEX": {"SENSEX", "SENSEX50"},
    }.get(name_u, {name_u})
    token_map: Dict[str, Dict[str, Any]] = {}

    filtered: List[Dict[str, Any]] = []
    for inst in instruments:
        exch = str(inst.get("exch_seg") or "").upper()
        if exch != exch_seg:
            continue
        if str(inst.get("instrumenttype") or "").upper() != "OPTIDX":
            continue
        sym = str(inst.get("symbol") or "").upper()
        nm = str(inst.get("name") or "").strip().upper()
        if not sym.endswith(("CE", "PE")):
            continue
        if nm not in allowed_inst_names:
            continue
        filtered.append(inst)

    # Prefer nearest calendar expiries. expiry_count=1 → front month/week only (matches typical “current week” NIFTY screen).
    # Drop expired series (IST calendar date). A stale master that still lists e.g. 17MAR2026 in April would otherwise
    # become the “front” expiry and chain LTPs won’t match the broker’s live weekly.
    expiries_raw = [str(x.get("expiry") or "").strip().upper() for x in filtered if str(x.get("expiry") or "").strip()]
    today_ist = datetime.now(_IST).date()
    expiry_dates: List[Tuple[datetime, str]] = []
    for e in set(expiries_raw):
        try:
            dt = datetime.strptime(e, "%d%b%Y")
        except ValueError:
            continue
        if dt.date() < today_ist:
            continue
        expiry_dates.append((dt, e))
    expiry_dates.sort(key=lambda x: x[0])
    exp_n = max(1, int(expiry_count))
    if not expiry_dates:
        logger.error(
            "[%s TOKENS] No option expiries on or after today (IST %s) in instrument master — refresh or delete %s",
            name_u,
            today_ist.isoformat(),
            INSTRUMENTS_PATH,
        )
        return [], {}
    selected_expiries = {x[1] for x in expiry_dates[:exp_n]}

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
            "expiry": exp or "",
        }

    def _expiry_sort_key(meta: Dict[str, Any]) -> tuple:
        es = str(meta.get("expiry") or "").strip().upper()
        try:
            return (datetime.strptime(es, "%d%b%Y"), str(meta.get("_tok", "")))
        except ValueError:
            return (datetime.max, str(meta.get("_tok", "")))

    # Balanced selection: prefer nearest strikes with both CE/PE coverage.
    strike_groups: Dict[float, Dict[str, List[Tuple[str, Dict[str, Any]]]]] = {}
    for tok, meta in token_map.items():
        s = float(meta["strike"])
        t = str(meta.get("type") or "")
        strike_groups.setdefault(s, {"CE": [], "PE": []})
        if t in ("CE", "PE"):
            m = dict(meta)
            m["_tok"] = tok
            strike_groups[s][t].append((tok, m))
    ordered_strikes = sorted(strike_groups.keys(), key=lambda s: abs(s - spot))
    balanced_items: List[Tuple[str, Dict[str, Any]]] = []
    for s in ordered_strikes:
        # Nearest expiry first so CE/PE LTP matches the weekly screen most traders watch.
        ce_list = sorted(strike_groups[s]["CE"], key=lambda x: _expiry_sort_key(x[1]))
        pe_list = sorted(strike_groups[s]["PE"], key=lambda x: _expiry_sort_key(x[1]))
        if ce_list:
            balanced_items.append((ce_list[0][0], {k: v for k, v in ce_list[0][1].items() if k != "_tok"}))
        if pe_list:
            balanced_items.append((pe_list[0][0], {k: v for k, v in pe_list[0][1].items() if k != "_tok"}))
        if len(balanced_items) >= int(max_tokens):
            break
    covered_strike_type = {(float(m["strike"]), str(m.get("type") or "")) for _t, m in balanced_items}
    if len(balanced_items) < int(max_tokens):
        used_tokens = {t for t, _ in balanced_items}
        extras = sorted(
            [
                (t, m)
                for t, m in token_map.items()
                if t not in used_tokens
                and (float(m["strike"]), str(m.get("type") or "")) not in covered_strike_type
            ],
            key=lambda kv: (abs(float(kv[1]["strike"]) - spot), kv[0]),
        )
        need = int(max_tokens) - len(balanced_items)
        for t, m in extras[:need]:
            balanced_items.append((t, m))
            covered_strike_type.add((float(m["strike"]), str(m.get("type") or "")))
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
    expiry_count: int = 1,
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
