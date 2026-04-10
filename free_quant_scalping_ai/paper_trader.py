"""
Aggregate-gated paper trading: opens only when platform aggregate + risk allow,
logs to logs/paper_trades.jsonl, exposes active / stats for the dashboard API.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services import option_chain_service
from utils.logger import get_logger

logger = get_logger(__name__)

_PAPER_LOG = Path(os.getenv("PAPER_TRADES_LOG", "logs/paper_trades.jsonl"))


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
_MIN_AGG_CONF = float(os.getenv("PAPER_AGG_MIN_CONF", "65"))
_BOOTSTRAP_KEY = "_paper_trades_bootstrapped"


def _default_paper_state() -> Dict[str, Any]:
    return {
        "active": {},
        "stats": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
        },
        "last_trade": None,
        "guide": {
            "entry": "Open when aggregate.signal is BUY_CE/BUY_PE, confidence >= 65, risk.passed (not blocked).",
            "exit": "Target hit or stop-loss hit.",
            "log": str(_PAPER_LOG),
        },
    }


def _risk_passed(risk: Any) -> bool:
    if not isinstance(risk, dict):
        return False
    if "passed" in risk:
        return bool(risk.get("passed"))
    return not bool(risk.get("blocked"))


def _leg_ltp(chain: Any, strike: float, opt: str) -> float:
    if not chain or not isinstance(chain, dict):
        return 0.0
    for key in (str(int(round(strike))), str(round(strike, 2)), str(strike)):
        row = chain.get(key)
        if not isinstance(row, dict):
            continue
        leg = row.get(opt)
        if isinstance(leg, dict):
            p = option_chain_service.option_leg_last_price(leg)
            return float(p or 0.0)
    return 0.0


def _append_jsonl(row: Dict[str, Any]) -> None:
    try:
        _PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _PAPER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("paper_trades jsonl append failed: %s", exc)


def _recompute_win_rate(stats: Dict[str, Any]) -> None:
    n = int(stats.get("total_trades") or 0)
    stats["win_rate"] = round(100.0 * float(stats.get("wins") or 0) / n, 2) if n else 0.0


def _system_health(stats: Dict[str, Any]) -> str:
    n = int(stats.get("total_trades") or 0)
    if n == 0:
        return "IDLE"
    if n < 5:
        return "WARMING_UP"
    wr = float(stats.get("win_rate") or 0.0)
    return "OK" if wr >= 40.0 else "WEAK"


def bootstrap(state: Dict[str, Any]) -> None:
    """Load cumulative stats from JSONL once (active positions stay empty)."""
    if state.get(_BOOTSTRAP_KEY):
        return
    state[_BOOTSTRAP_KEY] = True
    base = _default_paper_state()
    if not _PAPER_LOG.is_file():
        state["paper_trades"] = base
        return
    wins = losses = 0
    total_pnl = 0.0
    closes = 0
    try:
        with _PAPER_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "close":
                    continue
                closes += 1
                pnl = float(row.get("pnl") or 0.0)
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
    except OSError as exc:
        logger.warning("paper_trades bootstrap read failed: %s", exc)
        state["paper_trades"] = base
        return
    base["stats"].update(
        {
            "total_trades": closes,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 4),
        }
    )
    _recompute_win_rate(base["stats"])
    base["stats"]["system_health"] = _system_health(base["stats"])
    state["paper_trades"] = base
    logger.info(
        "[PAPER] Bootstrapped stats from %s: trades=%s pnl=%.2f win_rate=%s%%",
        _PAPER_LOG,
        closes,
        total_pnl,
        base["stats"]["win_rate"],
    )


def reset_paper_trades(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wipe the JSONL log and in-memory paper state (active, stats, last_trade).
    Clears bootstrap so the next bootstrap() reloads from the now-empty file.
    """
    try:
        _PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        _PAPER_LOG.write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("[PAPER] reset: could not truncate %s: %s", _PAPER_LOG, exc)
    state[_BOOTSTRAP_KEY] = False
    state["paper_trades"] = _default_paper_state()
    bootstrap(state)
    logger.info("[PAPER] Reset complete; log cleared at %s", _PAPER_LOG)
    return state.get("paper_trades") or _default_paper_state()


def _close_trade(
    state: Dict[str, Any],
    symbol: str,
    pos: Dict[str, Any],
    exit_prem: float,
    reason: str,
    index_price: float,
    aggregate_signal: str,
) -> None:
    pt = state.setdefault("paper_trades", _default_paper_state())
    entry = float(pos.get("entry") or 0.0)
    pnl = round(exit_prem - entry, 4)
    sig = str(pos.get("signal") or "")
    if reason == "TARGET":
        result = "FULL_TARGET"
    elif reason == "SL":
        result = "LOSS"
    elif reason == "REVERSE":
        result = "REVERSAL"
    else:
        result = str(reason)

    win = pnl > 0
    loss = pnl < 0
    st = pt.setdefault("stats", {})
    st["total_trades"] = int(st.get("total_trades") or 0) + 1
    if win:
        st["wins"] = int(st.get("wins") or 0) + 1
    elif loss:
        st["losses"] = int(st.get("losses") or 0) + 1
    st["total_pnl"] = round(float(st.get("total_pnl") or 0.0) + pnl, 4)
    _recompute_win_rate(st)
    st["system_health"] = _system_health(st)

    trade_id = pos.get("trade_id") or ""
    leg = pos.get("leg") or f'{symbol} {pos.get("strike_label") or ""}'.strip()
    closed = {
        "trade_id": trade_id,
        "symbol": symbol,
        "signal": sig,
        "strike": pos.get("strike_label"),
        "leg": leg,
        "type": pos.get("opt"),
        "entry": entry,
        "exit": round(exit_prem, 4),
        "pnl": pnl,
        "result": result,
        "exit_reason": reason,
        "closed_at": time.time(),
        "aggregate_signal_at_exit": aggregate_signal,
        "index_price_at_exit": index_price,
    }
    pt["last_trade"] = closed
    del pt["active"][symbol]

    _append_jsonl(
        {
            "event": "close",
            "ts": _utc_now_iso(),
            "symbol": symbol,
            "trade_id": trade_id,
            "entry": entry,
            "exit": round(exit_prem, 4),
            "pnl": pnl,
            "result": result,
            "reason": reason,
            "signal": sig,
        }
    )
    logger.info("[PAPER] CLOSE %s %s pnl=%s reason=%s", symbol, sig, pnl, reason)


def _maybe_open(
    state: Dict[str, Any],
    symbol: str,
    final_fast: Dict[str, Any],
    chain_snapshot: Any,
    aggregate: Dict[str, Any],
    risk: Any,
) -> None:
    pt = state.setdefault("paper_trades", _default_paper_state())
    if symbol in (pt.get("active") or {}):
        return

    sig = str(aggregate.get("signal") or "NO_TRADE").upper()
    if sig not in ("BUY_CE", "BUY_PE"):
        return
    try:
        conf = float(aggregate.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_AGG_CONF:
        return
    if not _risk_passed(risk):
        return

    price = float(final_fast.get("price") or 0.0)
    sel = option_chain_service.select_strike_for_scalp(chain_snapshot, price, sig)
    if not sel:
        return
    strike = float(sel["strike"])
    opt = str(sel.get("type") or ("CE" if sig == "BUY_CE" else "PE"))
    row = (
        chain_snapshot.get(str(int(strike)))
        or chain_snapshot.get(str(strike))
        or {}
    )
    leg = row.get(opt) if isinstance(row, dict) else None
    prem = option_chain_service.option_leg_last_price(leg) if leg else None
    if prem is None:
        prem = float(sel.get("premium") or 0.0)
    if prem <= 0:
        return
    entry = round(float(prem), 2)
    target = round(prem * 1.4, 2)
    stoploss = round(prem * 0.8, 2)
    trade_id = f"{symbol}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    strike_label = f"{int(strike)} {opt}"

    pos = {
        "trade_id": trade_id,
        "signal": sig,
        "opt": opt,
        "strike": strike,
        "strike_label": strike_label,
        "leg": f"{symbol} {strike_label}",
        "entry": entry,
        "target": target,
        "stoploss": stoploss,
        "opened_at": time.time(),
        "open_confidence": conf,
    }
    pt.setdefault("active", {})[symbol] = pos
    _append_jsonl(
        {
            "event": "open",
            "ts": _utc_now_iso(),
            "symbol": symbol,
            "trade_id": trade_id,
            "signal": sig,
            "strike": strike_label,
            "entry": entry,
            "target": target,
            "stoploss": stoploss,
            "aggregate_confidence": conf,
            "risk_passed": True,
        }
    )
    logger.info("[PAPER] OPEN %s %s @ %s tgt=%s sl=%s conf=%s", symbol, strike_label, entry, target, stoploss, conf)


def _update_active_position(
    state: Dict[str, Any],
    symbol: str,
    pos: Dict[str, Any],
    chain_snapshot: Any,
    final_fast: Dict[str, Any],
    aggregate: Dict[str, Any],
) -> bool:
    """Returns True if position was closed this call."""
    price = float(final_fast.get("price") or 0.0)
    strike = float(pos.get("strike") or 0.0)
    opt = str(pos.get("opt") or "CE")
    entry = float(pos.get("entry") or 0.0)
    target = float(pos.get("target") or 0.0)
    stoploss = float(pos.get("stoploss") or 0.0)
    ltp = _leg_ltp(chain_snapshot, strike, opt)
    if ltp <= 0:
        ltp = entry

    agg_sig = str(aggregate.get("signal") or "NO_TRADE").upper()
    if ltp <= stoploss:
        _close_trade(state, symbol, pos, ltp, "SL", price, agg_sig)
        return True
    if ltp >= target:
        _close_trade(state, symbol, pos, ltp, "TARGET", price, agg_sig)
        return True

    pt = state["paper_trades"]
    pnl_live = round(ltp - entry, 4)
    pnl_pct = round((pnl_live / entry) * 100.0, 2) if entry > 0 else 0.0
    pt["active"][symbol] = {
        **pos,
        "index_price": price,
        "last_prem": round(ltp, 4),
        "pnl_live": pnl_live,
        "pnl_percent": pnl_pct,
        "distance_to_target": round(target - ltp, 4),
        "distance_to_sl": round(ltp - stoploss, 4),
    }
    return False


def process_tick(symbol: str, final_fast: Dict[str, Any], chain_snapshot: Any, state: Dict[str, Any]) -> None:
    if not state.get(_BOOTSTRAP_KEY):
        bootstrap(state)

    aggregate = final_fast.get("platform_aggregate") or {}
    if not isinstance(aggregate, dict):
        aggregate = {}
    risk = final_fast.get("platform_risk") or {}
    if not isinstance(risk, dict):
        risk = {}
    pt = state.setdefault("paper_trades", _default_paper_state())
    pt.setdefault("active", {})
    pt.setdefault("stats", _default_paper_state()["stats"])
    st = pt["stats"]
    st.setdefault("system_health", _system_health(st))

    sym = str(symbol or "").upper()
    active = pt["active"].get(sym)

    closed = False
    if active:
        closed = _update_active_position(state, sym, active, chain_snapshot, final_fast, aggregate)

    if not closed and sym not in pt.get("active", {}):
        _maybe_open(state, sym, final_fast, chain_snapshot, aggregate, risk)

    st["system_health"] = _system_health(st)
