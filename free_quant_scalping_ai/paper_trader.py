"""
Paper trading mirrors ``ExecutionLifecycle`` (``_execution_trade`` + ``execution_final_signal``).

No independent strike/SL/target logic: entries follow execution CONFIRMED/OPEN, exits follow
EXIT / IDLE-after-close, PnL and trail/partials/reversal match the execution engine (scaled by
``PAPER_LOTS_PER_TRADE`` vs execution ``qty``).

Env:
  PAPER_USE_EXECUTION_ENGINE — default ``1`` (mirror). Set ``0`` to disable paper updates.
  PAPER_SYMBOLS — default ``NIFTY`` (comma list or ``*``).
  PAPER_LOTS_PER_TRADE / PAPER_MAX_LOTS — scale logged PnL vs execution qty.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_PAPER_LOG = Path(os.getenv("PAPER_TRADES_LOG", "logs/paper_trades.jsonl"))


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _paper_symbols() -> FrozenSet[str]:
    raw = os.getenv("PAPER_SYMBOLS", "NIFTY").strip().upper()
    if not raw or raw == "*":
        return frozenset({"NIFTY", "SENSEX"})
    return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _paper_lots() -> int:
    want = _env_int("PAPER_LOTS_PER_TRADE", 20)
    cap = _env_int("PAPER_MAX_LOTS", 20)
    return max(1, min(want, cap))


def _use_execution_mirror() -> bool:
    return os.getenv("PAPER_USE_EXECUTION_ENGINE", "1").strip().lower() in ("1", "true", "yes")


def _pnl_scale(exec_qty: int, paper_lots: int) -> float:
    q = max(1, int(exec_qty))
    return float(max(1, paper_lots)) / float(q)


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
            "mode": "execution_mirror",
            "PAPER_USE_EXECUTION_ENGINE": "1",
            "symbols": ",".join(sorted(_paper_symbols())),
            "source": "state['_execution_trade'] + state['execution_final_signal']",
            "exit": "Same as execution: SL_OR_TRAIL, TARGET, REVERSAL (trailing + partials in engine).",
            "lots_per_trade": _paper_lots(),
            "max_lots": _env_int("PAPER_MAX_LOTS", 20),
            "pnl_scale": "paper_pnl = execution_pnl * (PAPER_LOTS_PER_TRADE / max(exec_qty,1))",
            "log": str(_PAPER_LOG),
        },
    }


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


def _result_for_exit_reason(reason: str, pnl: float) -> str:
    r = str(reason or "").upper()
    if r == "TARGET":
        return "FULL_TARGET"
    if r in ("SL_OR_TRAIL", "SL"):
        return "LOSS" if pnl < 0 else "BREAKEVEN"
    if r == "REVERSAL":
        return "REVERSAL"
    return r or "CLOSE"


def _close_mirror_trade(
    state: Dict[str, Any],
    symbol: str,
    pos: Dict[str, Any],
    exit_prem: float,
    exec_reason: str,
    index_price: float,
    exit_signal: str,
    exec_pnl: float,
    exit_time_iso: str,
    trade_age_sec: float,
) -> None:
    pt = state.setdefault("paper_trades", _default_paper_state())
    entry = float(pos.get("entry_price") or pos.get("entry") or 0.0)
    lots = max(1, int(pos.get("lots") or 1))
    pnl = round(float(exec_pnl), 4)
    per_unit = round(pnl / max(lots, 1), 6) if lots else round(exit_prem - entry, 6)

    sig = str(pos.get("signal") or "")
    result = _result_for_exit_reason(exec_reason, pnl)

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
        "signal_source": "execution_mirror",
        "strike": pos.get("strike_label"),
        "leg": leg,
        "type": pos.get("opt"),
        "lots": lots,
        "exec_qty": pos.get("exec_qty"),
        "entry": entry,
        "exit": round(float(exit_prem), 4),
        "pnl": pnl,
        "pnl_per_lot": round(pnl / max(lots, 1), 4),
        "result": result,
        "exit_reason": exec_reason,
        "entry_time": pos.get("entry_time_iso"),
        "exit_time": exit_time_iso,
        "trade_age_sec": trade_age_sec,
        "closed_at": time.time(),
        "signal_at_exit": exit_signal,
        "aggregate_signal_at_exit": exit_signal,
        "index_price_at_exit": index_price,
        "remaining_fraction_at_exit": pos.get("remaining_fraction"),
        "trailing_sl_at_exit": pos.get("trailing_sl"),
    }
    pt["last_trade"] = closed
    del pt["active"][symbol]

    _append_jsonl(
        {
            "event": "close",
            "ts": exit_time_iso,
            "symbol": symbol,
            "trade_id": trade_id,
            "entry": entry,
            "exit": round(float(exit_prem), 4),
            "lots": lots,
            "exec_qty": pos.get("exec_qty"),
            "pnl": pnl,
            "pnl_per_lot": closed["pnl_per_lot"],
            "result": result,
            "reason": exec_reason,
            "signal": sig,
            "signal_source": "execution_mirror",
            "signal_at_exit": exit_signal,
            "entry_time": pos.get("entry_time_iso"),
            "exit_time": exit_time_iso,
            "trade_duration_sec": trade_age_sec,
            "trade_age_sec": trade_age_sec,
        }
    )
    logger.info("[PAPER] CLOSE(mirror) %s %s pnl=%s reason=%s", symbol, sig, pnl, exec_reason)


def _execution_bucket(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.setdefault("_execution_trade", {})


def _final_bucket(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.setdefault("execution_final_signal", {})


def _open_mirror_from_execution(
    state: Dict[str, Any],
    symbol: str,
    ex: Dict[str, Any],
    efs: Optional[Dict[str, Any]],
) -> None:
    pt = state.setdefault("paper_trades", _default_paper_state())
    if symbol in (pt.get("active") or {}):
        return
    if str(ex.get("status") or "").upper() != "OPEN":
        return

    lots = _paper_lots()
    qty = int(ex.get("qty") or 1)
    scale = _pnl_scale(qty, lots)
    entry_t = ex.get("entry_time")
    entry_iso = str(entry_t) if entry_t else _utc_now_iso()
    strike_label = str(ex.get("strike") or "")
    opt = str(ex.get("opt_type") or "CE").upper()
    sn = ex.get("strike_num")
    trade_id = f"{symbol}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    pos: Dict[str, Any] = {
        "trade_id": trade_id,
        "mirror_execution": True,
        "signal": str(ex.get("signal") or ""),
        "signal_source": "execution_mirror",
        "opt": opt,
        "strike": float(sn) if sn is not None else None,
        "strike_label": strike_label,
        "leg": f"{symbol} {strike_label}".strip(),
        "entry_price": float(ex.get("entry_price") or 0.0),
        "entry": float(ex.get("entry_price") or 0.0),
        "target": float(ex.get("target") or 0.0),
        "sl": float(ex.get("sl") or 0.0),
        "trailing_sl": float(ex.get("trailing_sl") or ex.get("sl") or 0.0),
        "lots": lots,
        "exec_qty": qty,
        "pnl_scale": scale,
        "opened_at": time.time(),
        "entry_time_iso": entry_iso,
        "exec_entry_time": entry_t,
        "open_confidence": float((efs or {}).get("confidence") or 0.0),
        "remaining_fraction": float(ex.get("remaining_fraction") or 1.0),
        "partial_booked": float(ex.get("partial_booked") or 0.0),
    }
    pt.setdefault("active", {})[symbol] = pos
    _append_jsonl(
        {
            "event": "open",
            "ts": entry_iso,
            "symbol": symbol,
            "trade_id": trade_id,
            "signal": pos["signal"],
            "signal_source": "execution_mirror",
            "strike": strike_label,
            "entry": pos["entry"],
            "target": pos["target"],
            "sl": pos["sl"],
            "trailing_sl": pos["trailing_sl"],
            "lots": lots,
            "exec_qty": qty,
            "entry_time": entry_iso,
            "confidence": pos["open_confidence"],
        }
    )
    logger.info("[PAPER] OPEN(mirror) %s %s exec_qty=%s paper_lots=%s", symbol, strike_label, qty, lots)


def _sync_mirror_open(state: Dict[str, Any], symbol: str, pos: Dict[str, Any], ex: Dict[str, Any]) -> None:
    """Refresh paper active row from execution OPEN state (trail, partials, pnl)."""
    pt = state["paper_trades"]
    scale = float(pos.get("pnl_scale") or 1.0)
    exec_pnl = float(ex.get("pnl") or 0.0)
    paper_pnl = round(exec_pnl * scale, 4)
    ent = float(ex.get("entry_price") or 0.0)
    cur = float(ex.get("current_price") or ent)
    opened = float(pos.get("opened_at") or time.time())
    trade_age_sec = round(time.time() - opened, 3)
    tgt = float(ex.get("target") or 0.0)
    tsl = float(ex.get("trailing_sl") or ex.get("sl") or 0.0)
    rem = float(ex.get("remaining_fraction") or 1.0)

    pt["active"][symbol] = {
        **pos,
        "entry_price": ent,
        "entry": ent,
        "current_price": cur,
        "target": tgt,
        "sl": float(ex.get("sl") or 0.0),
        "trailing_sl": tsl,
        "remaining_fraction": rem,
        "partial_booked": float(ex.get("partial_booked") or 0.0),
        "opposite_signal_count": int(ex.get("opposite_signal_count") or 0),
        "exec_pnl": exec_pnl,
        "pnl_live": paper_pnl,
        "pnl_percent": round(((cur - ent) / ent) * 100.0, 2) if ent > 0 else 0.0,
        "trade_age_sec": trade_age_sec,
        "last_prem": round(cur, 4),
        "distance_to_target": round(tgt - cur, 4) if tgt else None,
        "distance_to_sl": round(cur - tsl, 4) if tsl else None,
    }


def _try_close_mirror(
    state: Dict[str, Any],
    symbol: str,
    pos: Dict[str, Any],
    ex: Dict[str, Any],
    efs: Optional[Dict[str, Any]],
    index_price: float,
    fast_signal: str,
) -> bool:
    """Close paper when execution has left OPEN (same tick EXIT or subsequent IDLE + last_exit)."""
    st_ex = str(ex.get("status") or "").upper()
    if st_ex == "OPEN":
        return False

    exit_from_final = isinstance(efs, dict) and str(efs.get("status") or "").upper() == "EXIT"
    if not exit_from_final and not ex.get("last_exit_reason"):
        return False

    exit_reason = str(ex.get("last_exit_reason") or "")
    exit_px = ex.get("last_exit_option_px")
    exit_time_iso = _utc_now_iso()
    exec_pnl_scaled: Optional[float] = None

    if isinstance(efs, dict) and str(efs.get("status") or "").upper() == "EXIT":
        try:
            exit_px = float(efs.get("ltp") if efs.get("ltp") is not None else exit_px)
        except (TypeError, ValueError):
            pass
        exit_reason = str(efs.get("reason") or exit_reason)
        t = efs.get("time")
        if t:
            exit_time_iso = str(t)
        try:
            raw_pnl = float(efs.get("pnl") or 0.0)
            scale = float(pos.get("pnl_scale") or 1.0)
            exec_pnl_scaled = round(raw_pnl * scale, 4)
        except (TypeError, ValueError):
            exec_pnl_scaled = None

    if exit_px is None:
        try:
            exit_px = float(pos.get("current_price") or pos.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            exit_px = 0.0
    else:
        try:
            exit_px = float(exit_px)
        except (TypeError, ValueError):
            exit_px = float(pos.get("current_price") or 0.0)

    if exec_pnl_scaled is None:
        ent = float(pos.get("entry_price") or 0.0)
        rem = float(pos.get("remaining_fraction") or 1.0)
        qty = max(1, int(pos.get("exec_qty") or 1))
        scale = float(pos.get("pnl_scale") or 1.0)
        raw_exec = (float(exit_px) - ent) * rem * float(qty)
        exec_pnl_scaled = round(raw_exec * scale, 4)

    opened = float(pos.get("opened_at") or time.time())
    trade_age_sec = round(time.time() - opened, 3)
    try:
        et = pos.get("entry_time_iso")
        if et:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            trade_age_sec = round((datetime.now(timezone.utc) - dt).total_seconds(), 3)
    except (TypeError, ValueError):
        pass

    _close_mirror_trade(
        state,
        symbol,
        pos,
        exit_prem=float(exit_px),
        exec_reason=exit_reason or "EXIT",
        index_price=index_price,
        exit_signal=fast_signal,
        exec_pnl=float(exec_pnl_scaled),
        exit_time_iso=exit_time_iso,
        trade_age_sec=trade_age_sec,
    )
    return True


def process_tick(symbol: str, final_fast: Dict[str, Any], chain_snapshot: Any, state: Dict[str, Any]) -> None:
    del chain_snapshot  # execution engine already used chain; mirror only reads state
    sym = str(symbol or "").upper()
    if sym not in _paper_symbols():
        return
    if not _use_execution_mirror():
        return

    if not state.get(_BOOTSTRAP_KEY):
        bootstrap(state)

    pt = state.setdefault("paper_trades", _default_paper_state())
    pt.setdefault("active", {})
    pt.setdefault("stats", _default_paper_state()["stats"])
    st = pt["stats"]
    st.setdefault("system_health", _system_health(st))

    ex = _execution_bucket(state).get(sym) or {}
    efs = _final_bucket(state).get(sym)
    index_price = float((final_fast or {}).get("price") or 0.0)
    fast_signal = str((final_fast or {}).get("signal") or "NO_TRADE").upper()

    active = pt["active"].get(sym)

    if active and active.get("mirror_execution"):
        if str(ex.get("status") or "").upper() == "OPEN":
            _sync_mirror_open(state, sym, active, ex)
        else:
            _try_close_mirror(state, sym, active, ex, efs if isinstance(efs, dict) else None, index_price, fast_signal)
        st["system_health"] = _system_health(st)
        return

    # New mirror session: execution OPEN (CONFIRMED final signal is emitted same tick).
    if str(ex.get("status") or "").upper() == "OPEN":
        _open_mirror_from_execution(state, sym, ex, efs if isinstance(efs, dict) else None)

    st["system_health"] = _system_health(st)
