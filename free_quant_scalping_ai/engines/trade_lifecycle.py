"""
Production-style trade lifecycle: IDLE → OPEN → CLOSED with exits, partials, trailing SL,
and reversal exit only after sustained opposite signal + confirmation.

State is stored in ``global_state["_execution_trade"][symbol]`` (plain dict, JSON-friendly).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.services import option_chain_service

_TRADE_ANALYTICS_PATH = Path(os.getenv("TRADE_ANALYTICS_FILE", "logs/trade_analytics.jsonl"))


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
        "entry_reason": None,
        "last_exit_reason": None,
        "max_pnl_seen": None,
        "drawdown_max": None,
        "_last_hold_emit_ts": 0.0,
        "_last_no_trade_emit_ts": 0.0,
        "_last_no_trade_reason": None,
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
    stage: str = "ENTRY",
) -> Dict[str, Any]:
    return {
        "status": status,
        "stage": stage,
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


def _seconds_since_entry(entry_iso: Optional[str]) -> float:
    if not entry_iso:
        return 1e9
    try:
        s = str(entry_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (TypeError, ValueError):
        return 1e9


def _append_trade_analytics(row: Dict[str, Any]) -> None:
    try:
        _TRADE_ANALYTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _TRADE_ANALYTICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
    except OSError:
        pass


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
        self.min_profit_to_trail = _env_float("EXEC_MIN_PROFIT_TO_TRAIL", 7.5)
        self.rev_min_hold_sec = _env_float("EXEC_REVERSAL_MIN_HOLD_SEC", 15.0)
        self.rev_inprofit_count_delta = _env_int("EXEC_REVERSAL_INPROFIT_COUNT_DELTA", 1)
        self.rev_inprofit_conf_delta = _env_float("EXEC_REVERSAL_INPROFIT_CONF_DELTA", 5.0)
        self.sl_reentry_conf_bump = _env_float("EXEC_SL_REENTRY_CONF_BUMP", 10.0)
        self.hold_emit_interval = _env_float("EXEC_HOLD_SIGNAL_INTERVAL_SEC", 12.0)
        self.hold_trail_eps = _env_float("EXEC_HOLD_TRAIL_CHANGE_EPS", 0.03)
        self.no_trade_emit_interval = _env_float("EXEC_NO_TRADE_EMIT_INTERVAL_SEC", 20.0)

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
        skip_entry_reason: Optional[str] = None,
        entry_context_reason: Optional[str] = None,
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
            unreal_pts = float(ltp) - float(ent)
            # Trailing from peak only after minimum profit (premium points) is reached
            if unreal_pts > self.min_profit_to_trail:
                trail_candidate = peak_f * (1.0 - self.trail_pct / 100.0) if peak_f > ent else sl
                trail_sl_f = max(sl, trail_candidate, trail_sl_f)
            else:
                trail_sl_f = float(sl)
            ts["trailing_sl"] = round(trail_sl_f, 4)

            rem = float(ts.get("remaining_fraction") or 1.0)
            qty = int(ts.get("qty") or 0)
            pnl_now = round((ltp - ent) * rem * max(1, qty), 4)
            ts["pnl"] = pnl_now
            mx = float(ts.get("max_pnl_seen") if ts.get("max_pnl_seen") is not None else pnl_now)
            if pnl_now > mx:
                mx = pnl_now
            ts["max_pnl_seen"] = round(mx, 4)
            dd = float(ts.get("drawdown_max") or 0.0)
            dd = max(dd, mx - pnl_now)
            ts["drawdown_max"] = round(dd, 4)

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
            opp_side = _opposite_signal(open_sig, fast) or _opposite_signal(open_sig, agg)
            entry_idx = float(ts.get("entry_index_price") or index_price)
            if open_sig == "BUY_CE":
                idx_move_against = entry_idx - index_price
            elif open_sig == "BUY_PE":
                idx_move_against = index_price - entry_idx
            else:
                idx_move_against = 0.0

            hold_sec = _seconds_since_entry(ts.get("entry_time"))
            hold_ok = hold_sec >= self.rev_min_hold_sec
            in_profit = float(ltp) > float(ent)
            need_rev = self.rev_count + (self.rev_inprofit_count_delta if in_profit else 0)
            need_conf = self.rev_conf + (self.rev_inprofit_conf_delta if in_profit else 0.0)

            if open_sig and (fast == open_sig or agg == open_sig):
                ts["opposite_signal_count"] = 0
            elif opp_side and hold_ok:
                opp_conf = float(aggregate_confidence) if _opposite_signal(open_sig, agg) else float(confidence)
                if opp_conf >= need_conf and idx_move_against >= self.rev_index_pts:
                    ts["opposite_signal_count"] = int(ts.get("opposite_signal_count") or 0) + 1

            if hold_ok and int(ts.get("opposite_signal_count") or 0) >= need_rev:
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
                prev_tr = ts.get("_last_emit_trailing_sl")
                now_t = time.time()
                trail_changed = prev_tr is None or abs(float(prev_tr) - trail_sl_f) >= self.hold_trail_eps
                if trail_changed or (now_t - float(ts.get("_last_hold_emit_ts") or 0.0)) >= self.hold_emit_interval:
                    ts["_last_hold_emit_ts"] = now_t
                    ts["_last_emit_trailing_sl"] = trail_sl_f
                    final_bucket[sym] = _build_final_signal(
                        status="CONFIRMED",
                        stage="HOLD",
                        symbol=sym,
                        signal=open_sig or None,
                        strike=str(ts.get("strike") or ""),
                        entry=float(ent),
                        ltp=float(ltp),
                        target=float(tgt),
                        sl=float(sl),
                        trailing_sl=float(trail_sl_f),
                        pnl=float(ts["pnl"]),
                        confidence=float(confidence),
                        reason="position_open",
                    )
                self._store(state, sym, ts)
            return

        # --- IDLE / CLOSED: entry ---
        if ts["status"] not in ("IDLE", "CLOSED"):
            return

        want = str(entry_decision or "").upper()
        if want not in ("BUY_CE", "BUY_PE"):
            return

        eff_min_conf = self.min_conf
        if str(ts.get("last_exit_reason") or "") == "SL_OR_TRAIL":
            eff_min_conf += self.sl_reentry_conf_bump
        if float(confidence or 0) < eff_min_conf:
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

        if skip_entry_reason:
            now_t = time.time()
            last_r = ts.get("_last_no_trade_reason")
            last_emit = float(ts.get("_last_no_trade_emit_ts") or 0.0)
            if skip_entry_reason != last_r or (now_t - last_emit) >= self.no_trade_emit_interval:
                ts["_last_no_trade_reason"] = skip_entry_reason
                ts["_last_no_trade_emit_ts"] = now_t
                self._store(state, sym, ts)
                final_bucket[sym] = _build_final_signal(
                    status="NO_TRADE",
                    stage="ENTRY",
                    symbol=sym,
                    signal=want,
                    strike=strike_label,
                    entry=float(entry) if entry is not None else None,
                    ltp=float(entry) if entry is not None else None,
                    target=float(target) if target is not None else None,
                    sl=float(stoploss) if stoploss is not None else None,
                    trailing_sl=float(stoploss) if stoploss is not None else None,
                    pnl=0.0,
                    confidence=float(confidence),
                    reason=skip_entry_reason,
                )
            return

        qty = self._qty_for_risk(float(entry), float(stoploss))
        now_iso = datetime.now(timezone.utc).isoformat()
        er = str(entry_context_reason or "entry_confirmed")
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
                "entry_reason": er,
                "max_pnl_seen": 0.0,
                "drawdown_max": 0.0,
            }
        )
        self._store(state, sym, new_ts)
        final_bucket[sym] = _build_final_signal(
            status="CONFIRMED",
            stage="ENTRY",
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
            stage="EXIT",
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
        et = ts.get("entry_time")
        xt = ts.get("exit_time")
        dur = _seconds_since_entry(et) if et else None
        _append_trade_analytics(
            {
                "symbol": symbol,
                "entry_time": et,
                "exit_time": xt,
                "entry_reason": ts.get("entry_reason"),
                "exit_reason": reason,
                "pnl": round(float(pnl), 4),
                "trade_duration_sec": round(float(dur), 3) if dur is not None else None,
                "max_pnl": ts.get("max_pnl_seen"),
                "drawdown": ts.get("drawdown_max"),
            }
        )
        # Reset to IDLE shell (keep last exit fields for re-entry)
        idle = default_trade_state()
        idle["status"] = "IDLE"
        idle["last_exit_underlying"] = ts["last_exit_underlying"]
        idle["last_exit_option_px"] = ts["last_exit_option_px"]
        idle["last_exit_signal"] = ts["last_exit_signal"]
        idle["last_exit_reason"] = reason
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
