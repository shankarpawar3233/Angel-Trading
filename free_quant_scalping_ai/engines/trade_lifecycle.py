"""
Production-style trade lifecycle: IDLE → OPEN → CLOSED with exits, partials, trailing SL,
and reversal exit only after sustained opposite signal + confirmation.

State is stored in ``global_state["_execution_trade"][symbol]`` (plain dict, JSON-friendly).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.services import option_chain_service


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def default_trade_state() -> Dict[str, Any]:
    return {
        "status": "IDLE",
        "signal": None,
        "strike": None,
        "strike_num": None,
        "opt_type": None,
        "entry_price": None,
        "exit_price": None,
        "current_price": None,
        "target": None,
        "sl": None,
        "trailing_sl": None,
        "qty": 0,
        "pnl": 0.0,
        "entry_time": None,
        "exit_time": None,
        "exit_reason": None,
        "opposite_signal_count": 0,
        "partial_booked": 0.0,
        "last_exit_underlying": None,
        "last_exit_option_px": None,
        "last_exit_signal": None,
        "peak_ltp": None,
        "remaining_fraction": 1.0,
        "partial_50_done": False,
        "partial_70_done": False,
        "entry_index_price": None,
    }


def _opposite_signal(open_signal: str, candidate: str) -> bool:
    o = str(open_signal or "").upper()
    c = str(candidate or "").upper()
    if o == "BUY_CE" and c == "BUY_PE":
        return True
    if o == "BUY_PE" and c == "BUY_CE":
        return True
    return False


def _build_final_signal(
    *,
    status: str,
    symbol: str,
    signal: Optional[str],
    strike: Optional[str],
    entry: Optional[float],
    ltp: Optional[float],
    target: Optional[float],
    sl: Optional[float],
    trailing_sl: Optional[float],
    pnl: Optional[float],
    confidence: Optional[float],
    reason: str,
) -> Dict[str, Any]:
    return {
        "status": status,
        "symbol": symbol,
        "signal": signal,
        "strike": strike,
        "entry": entry,
        "ltp": ltp,
        "target": target,
        "sl": sl,
        "trailing_sl": trailing_sl,
        "pnl": round(float(pnl or 0.0), 4),
        "confidence": confidence,
        "reason": reason,
        "time": datetime.now(timezone.utc).isoformat(),
    }


def _leg_ltp_and_age(
    chain: Dict[str, Any], strike: float, opt: str
) -> Tuple[Optional[float], Optional[float]]:
    row = option_chain_service.chain_row_for_strike(chain, float(strike))
    leg = row.get(opt) if isinstance(row, dict) else None
    if not isinstance(leg, dict):
        return None, None
    ltp = option_chain_service.option_leg_last_price(leg)
    ts = leg.get("ts")
    age = None
    if ts is not None:
        try:
            age = max(0.0, time.time() - float(ts))
        except (TypeError, ValueError):
            age = None
    return ltp, age


class ExecutionLifecycle:
    """
    Per-symbol execution state machine. Mutates ``state["_execution_trade"]`` and
    publishes ``state["execution_final_signal"][symbol]`` only on CONFIRMED / EXIT.
    """

    def __init__(self) -> None:
        self.capital = _env_float("EXEC_NOTIONAL_CAPITAL", 500000.0)
        self.risk_pct = _env_float("EXEC_RISK_PER_TRADE_PCT", 0.5)
        self.max_ltp_age = _env_float("ENTRY_MAX_LTP_AGE_SEC", 1.0)
        self.min_conf = _env_float("EXEC_MIN_ENTRY_CONFIDENCE", 68.0)
        self.reentry_move = _env_float("EXEC_REENTRY_MIN_UNDERLYING_MOVE", 15.0)
        self.rev_count = _env_int("EXEC_REVERSAL_OPPOSITE_COUNT", 3)
        self.rev_conf = _env_float("EXEC_REVERSAL_OPPOSITE_MIN_CONF", 72.0)
        self.rev_index_pts = _env_float("EXEC_REVERSAL_INDEX_POINTS", 12.0)
        self.trail_pct = _env_float("EXEC_TRAILING_GIVEBACK_PCT", 0.35)
        self.partial_50_frac = _env_float("EXEC_PARTIAL_AT_50PCT", 0.30)
        self.partial_70_frac = _env_float("EXEC_PARTIAL_AT_70PCT", 0.30)

    def _store(self, state: Dict[str, Any], symbol: str, ts: Dict[str, Any]) -> None:
        state.setdefault("_execution_trade", {})[symbol] = ts

    def _get(self, state: Dict[str, Any], symbol: str) -> Dict[str, Any]:
        bucket = state.setdefault("_execution_trade", {})
        if symbol not in bucket:
            bucket[symbol] = default_trade_state()
        return bucket[symbol]

    def _qty_for_risk(self, entry: float, sl: float) -> int:
        risk_amt = self.capital * (self.risk_pct / 100.0)
        per_unit = abs(float(entry) - float(sl))
        if per_unit <= 1e-6:
            return 1
        q = int(risk_amt / per_unit)
        return max(1, min(q, _env_int("EXEC_MAX_QTY", 500)))

    def process_tick(
        self,
        state: Dict[str, Any],
        symbol: str,
        *,
        index_price: float,
        chain_snapshot: Dict[str, Dict[str, Dict[str, Any]]],
        scalping_signal: str,
        entry_decision: str,
        confidence: float,
        aggregate_signal: str,
        aggregate_confidence: float,
        strike_label: Optional[str],
        strike_num: Optional[float],
        opt_type: Optional[str],
        entry: Optional[float],
        target: Optional[float],
        stoploss: Optional[float],
        chain_ltp_age_sec: Optional[float],
    ) -> None:
        sym = symbol.upper()
        ts = self._get(state, sym)
        final_bucket = state.setdefault("execution_final_signal", {})

        # --- OPEN: manage position ---
        if ts["status"] == "OPEN" and ts.get("strike_num") is not None and ts.get("opt_type"):
            sn = float(ts["strike_num"])
            ot = str(ts["opt_type"])
            ltp, age = _leg_ltp_and_age(chain_snapshot, sn, ot)
            if ltp is None or ltp <= 0:
                ltp = float(ts.get("current_price") or ts.get("entry_price") or 0.0)
            ts["current_price"] = round(float(ltp), 4)
            ent = float(ts["entry_price"] or 0.0)
            tgt = float(ts["target"] or 0.0)
            sl = float(ts["sl"] or 0.0)
            trail_sl = ts.get("trailing_sl")
            trail_sl_f = float(trail_sl) if trail_sl is not None else sl

            peak = ts.get("peak_ltp")
            peak_f = float(peak) if peak is not None else ent
            if ltp > peak_f:
                peak_f = ltp
                ts["peak_ltp"] = peak_f
            # Trailing: lock giveback from peak
            trail_candidate = peak_f * (1.0 - self.trail_pct / 100.0) if peak_f > ent else sl
            trail_sl_f = max(sl, trail_candidate, trail_sl_f)
            ts["trailing_sl"] = round(trail_sl_f, 4)

            rem = float(ts.get("remaining_fraction") or 1.0)
            qty = int(ts.get("qty") or 0)
            ts["pnl"] = round((ltp - ent) * rem * max(1, qty), 4)

            # Hard SL (use trailing)
            if ltp <= trail_sl_f:
                self._close(
                    state,
                    sym,
                    ts,
                    float(ltp),
                    "SL_OR_TRAIL",
                    final_bucket,
                    confidence,
                    index_price,
                )
                return

            # Target / partials (distance from entry to target)
            span = tgt - ent
            if span > 0:
                frac_to_tgt = (ltp - ent) / span
                if frac_to_tgt >= 0.50 and not ts.get("partial_50_done"):
                    ts["partial_50_done"] = True
                    ts["remaining_fraction"] = round(rem * (1.0 - self.partial_50_frac), 4)
                    ts["partial_booked"] = round(
                        float(ts.get("partial_booked") or 0.0) + self.partial_50_frac * 100.0, 2
                    )
                    rem = float(ts["remaining_fraction"])
                if frac_to_tgt >= 0.70 and not ts.get("partial_70_done"):
                    ts["partial_70_done"] = True
                    ts["remaining_fraction"] = round(float(ts["remaining_fraction"]) * (1.0 - self.partial_70_frac), 4)
                    ts["partial_booked"] = round(
                        float(ts.get("partial_booked") or 0.0) + self.partial_70_frac * 100.0, 2
                    )
                    rem = float(ts["remaining_fraction"])
                if ltp >= tgt and rem > 1e-6:
                    self._close(state, sym, ts, float(ltp), "TARGET", final_bucket, confidence, index_price)
                    return

            # Reversal: count sustained opposite; reset when same-side resumes
            open_sig = str(ts.get("signal") or "").upper()
            fast = str(scalping_signal or "").upper()
            agg = str(aggregate_signal or "").upper()
            same_side = fast == open_sig or agg == open_sig
            opp_side = _opposite_signal(open_sig, fast) or _opposite_signal(open_sig, agg)
            entry_idx = float(ts.get("entry_index_price") or index_price)
            if open_sig == "BUY_CE":
                idx_move_against = entry_idx - index_price
            elif open_sig == "BUY_PE":
                idx_move_against = index_price - entry_idx
            else:
                idx_move_against = 0.0

            if open_sig and (fast == open_sig or agg == open_sig):
                ts["opposite_signal_count"] = 0
            elif opp_side:
                opp_conf = float(aggregate_confidence) if _opposite_signal(open_sig, agg) else float(confidence)
                if opp_conf >= self.rev_conf and idx_move_against >= self.rev_index_pts:
                    ts["opposite_signal_count"] = int(ts.get("opposite_signal_count") or 0) + 1

            if int(ts.get("opposite_signal_count") or 0) >= self.rev_count:
                self._close(
                    state,
                    sym,
                    ts,
                    float(ltp),
                    "REVERSAL",
                    final_bucket,
                    confidence,
                    index_price,
                )
            else:
                self._store(state, sym, ts)
            return

        # --- IDLE / CLOSED: entry ---
        if ts["status"] not in ("IDLE", "CLOSED"):
            return

        want = str(entry_decision or "").upper()
        if want not in ("BUY_CE", "BUY_PE"):
            return
        if float(confidence or 0) < self.min_conf:
            return
        if strike_num is None or opt_type is None or entry is None or target is None or stoploss is None:
            return
        if chain_ltp_age_sec is not None and float(chain_ltp_age_sec) > self.max_ltp_age:
            return

        # Re-entry guard
        if ts["status"] == "CLOSED":
            last_u = ts.get("last_exit_underlying")
            last_sig = str(ts.get("last_exit_signal") or "")
            if last_u is not None:
                if abs(float(index_price) - float(last_u)) < self.reentry_move:
                    return
            if last_sig and last_sig != want:
                return

        qty = self._qty_for_risk(float(entry), float(stoploss))
        now_iso = datetime.now(timezone.utc).isoformat()
        new_ts = default_trade_state()
        new_ts.update(
            {
                "status": "OPEN",
                "signal": want,
                "strike": strike_label,
                "strike_num": float(strike_num),
                "opt_type": str(opt_type),
                "entry_price": float(entry),
                "current_price": float(entry),
                "target": float(target),
                "sl": float(stoploss),
                "trailing_sl": float(stoploss),
                "qty": qty,
                "pnl": 0.0,
                "entry_time": now_iso,
                "peak_ltp": float(entry),
                "entry_index_price": float(index_price),
                "remaining_fraction": 1.0,
                "partial_booked": 0.0,
                "opposite_signal_count": 0,
            }
        )
        self._store(state, sym, new_ts)
        final_bucket[sym] = _build_final_signal(
            status="CONFIRMED",
            symbol=sym,
            signal=want,
            strike=strike_label,
            entry=float(entry),
            ltp=float(entry),
            target=float(target),
            sl=float(stoploss),
            trailing_sl=float(stoploss),
            pnl=0.0,
            confidence=float(confidence),
            reason="entry_confirmed",
        )

    def _close(
        self,
        state: Dict[str, Any],
        symbol: str,
        ts: Dict[str, Any],
        exit_px: float,
        reason: str,
        final_bucket: Dict[str, Any],
        confidence: float,
        index_price: float,
    ) -> None:
        ent = float(ts.get("entry_price") or 0.0)
        rem = float(ts.get("remaining_fraction") or 1.0)
        qty = int(ts.get("qty") or 1)
        pnl = (exit_px - ent) * rem * max(1, qty)
        ts["status"] = "CLOSED"
        ts["exit_price"] = round(exit_px, 4)
        ts["exit_time"] = datetime.now(timezone.utc).isoformat()
        ts["exit_reason"] = reason
        ts["pnl"] = round(pnl, 4)
        ts["last_exit_underlying"] = float(index_price)
        ts["last_exit_option_px"] = round(exit_px, 4)
        ts["last_exit_signal"] = str(ts.get("signal") or "")
        final_bucket[symbol] = _build_final_signal(
            status="EXIT",
            symbol=symbol,
            signal=str(ts.get("signal") or ""),
            strike=str(ts.get("strike") or ""),
            entry=ent,
            ltp=round(exit_px, 4),
            target=ts.get("target"),
            sl=ts.get("sl"),
            trailing_sl=ts.get("trailing_sl"),
            pnl=pnl,
            confidence=float(confidence),
            reason=reason,
        )
        # Reset to IDLE shell (keep last exit fields for re-entry)
        idle = default_trade_state()
        idle["status"] = "IDLE"
        idle["last_exit_underlying"] = ts["last_exit_underlying"]
        idle["last_exit_option_px"] = ts["last_exit_option_px"]
        idle["last_exit_signal"] = ts["last_exit_signal"]
        self._store(state, symbol, idle)


def parse_strike_label(label: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    if not label or not isinstance(label, str):
        return None, None
    parts = label.strip().split()
    if len(parts) < 2:
        return None, None
    try:
        return float(parts[0]), str(parts[1]).upper()
    except ValueError:
        return None, None
