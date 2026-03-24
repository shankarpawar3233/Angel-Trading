"""
Paper trading simulator: read-only sidecar. No broker, no impact on signal path.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Per-underlying lot size (exchange); total contracts = lot_size * NO_OF_LOTS
_LOT_SIZE_BY_SYMBOL = {"NIFTY": 65, "SENSEX": 20}
NO_OF_LOTS = 20

_PAPER_LOG = Path("logs/paper_trades.jsonl")
_active: Dict[str, Dict[str, Any]] = {}
_stats: Dict[str, Any] = {
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "total_profit": 0.0,
    "total_loss": 0.0,
    "total_duration": 0.0,
    "max_profit": 0.0,
    "max_loss": 0.0,
    "current_streak_count": 0,
    "current_streak_type": None,
    "best_streak": 0,
}
_last_trade: Optional[Dict[str, Any]] = None
_last_exit_ts: Dict[str, float] = {}
_entry_ts: Dict[str, list[float]] = {}

_DASHBOARD_GUIDE: Dict[str, str] = {
    "active": "Live running trades with real-time PnL",
    "pnl_live": "Current profit/loss based on option premium",
    "win_rate": "Winning trades percentage",
    "risk_reward": "Average profit vs average loss",
    "system_health": "Overall strategy strength",
    "distance_to_target": "Remaining move to hit target",
    "distance_to_sl": "Buffer before stoploss hit",
}

# O(1) re-entry memory when flat: two prior spots / premiums for direction + momentum
_flat_hist: Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]] = {}


def _quantity_for(symbol: str) -> int:
    u = symbol.upper()
    lot = _LOT_SIZE_BY_SYMBOL.get(u, _LOT_SIZE_BY_SYMBOL["NIFTY"])
    return int(lot * NO_OF_LOTS)


def _lots_for_confidence(conf: float) -> int:
    if conf >= 90:
        return 25
    if conf >= 85:
        return 15
    return 8


def _build_dashboard_stats() -> Dict[str, Any]:
    """O(1) derived metrics from stored counters only."""
    tt = int(_stats["total_trades"])
    w = int(_stats["wins"])
    l = int(_stats["losses"])
    tprof = float(_stats["total_profit"])
    tloss = float(_stats["total_loss"])
    win_rate = (w / tt) * 100.0 if tt > 0 else 0.0
    avg_profit = tprof / w if w > 0 else 0.0
    avg_loss = tloss / l if l > 0 else 0.0
    risk_reward = abs(avg_profit / avg_loss) if avg_loss != 0 else 0.0
    if win_rate >= 60.0:
        health = "STRONG"
    elif win_rate >= 45.0:
        health = "MODERATE"
    else:
        health = "WEAK"
    return {
        "total_trades": tt,
        "wins": w,
        "losses": l,
        "total_pnl": float(_stats["total_pnl"]),
        "total_profit": tprof,
        "total_loss": tloss,
        "win_rate": win_rate,
        "avg_profit": avg_profit,
        "avg_loss": avg_loss,
        "risk_reward": risk_reward,
        "system_health": health,
    }


def _build_dashboard_insights() -> Dict[str, Any]:
    """O(1) insights from incrementally maintained counters only."""
    tt = int(_stats["total_trades"])
    w = int(_stats["wins"])
    tloss = float(_stats["total_loss"])
    tprof = float(_stats["total_profit"])
    total_duration = float(_stats.get("total_duration", 0.0))
    avg_duration = (total_duration / tt) if tt > 0 else 0.0
    profit_factor = (tprof / abs(tloss)) if tloss != 0 else 0.0
    win_rate = (w / tt) * 100.0 if tt > 0 else 0.0
    ctype = _stats.get("current_streak_type")
    ccount = int(_stats.get("current_streak_count") or 0)
    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_duration": avg_duration,
        "max_profit_trade": float(_stats.get("max_profit", 0.0)),
        "max_loss_trade": float(_stats.get("max_loss", 0.0)),
        "current_streak": {"type": ctype, "count": ccount},
        "best_streak": int(_stats.get("best_streak", 0)),
    }


def get_paper_stats() -> Dict[str, Any]:
    """Read-only snapshot for dashboards / debugging."""
    return {"stats": _build_dashboard_stats(), "insights": _build_dashboard_insights()}


def _strike_key_side(strike_label: Any) -> tuple[Optional[str], Optional[str]]:
    if not strike_label or not isinstance(strike_label, str):
        return None, None
    parts = strike_label.strip().split()
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1].upper()


def _ltp(chain: Any, strike_key: str, side: str) -> Optional[float]:
    if not chain or not isinstance(chain, dict):
        return None
    row = chain.get(strike_key)
    if not isinstance(row, dict):
        return None
    leg = row.get(side)
    if not isinstance(leg, dict):
        return None
    raw = leg.get("ltp")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _append_closed(row: Dict[str, Any]) -> None:
    try:
        _PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _PAPER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _update_flat_hist(symbol: str, spot: Optional[float], prem: Optional[float]) -> None:
    h = _flat_hist.setdefault(symbol, {"spot": (None, None), "prem": (None, None)})
    s0, s1 = h["spot"]
    h["spot"] = (s1, spot)
    p0, p1 = h["prem"]
    h["prem"] = (p1, prem)


def _reentry_ok(symbol: str, ed: str, spot: Optional[float], prem: Optional[float]) -> bool:
    if spot is None or prem is None:
        return True
    h = _flat_hist.get(symbol)
    if not h:
        return True
    s0, s1 = h["spot"]
    p0, p1 = h["prem"]
    if s0 is None or s1 is None or p0 is None or p1 is None:
        return True
    if ed == "BUY_CE":
        if not (spot > s1 > s0):
            return False
    elif ed == "BUY_PE":
        if not (spot < s1 < s0):
            return False
    return prem > p1 > p0


def _close_trade(
    symbol: str,
    active: Dict[str, Any],
    exit_px: float,
    result: str,
) -> None:
    entry_px = float(active["entry"])
    qty = int(active["quantity"])
    pnl = (exit_px - entry_px) * qty
    exit_time = datetime.now(timezone.utc).isoformat()
    entry_time = active.get("entry_time")
    duration_sec: Optional[float] = None
    if isinstance(entry_time, str):
        try:
            et = datetime.fromisoformat(entry_time)
            xt = datetime.fromisoformat(exit_time)
            duration_sec = max(0.0, (xt - et).total_seconds())
        except Exception:
            duration_sec = None
    _stats["total_trades"] += 1
    if result == "FULL_TARGET":
        _stats["wins"] += 1
    elif result == "TRAIL_EXIT":
        if pnl > 0:
            _stats["wins"] += 1
        else:
            _stats["losses"] += 1
    else:
        _stats["losses"] += 1
    _stats["total_pnl"] += pnl
    if pnl > 0:
        _stats["total_profit"] += pnl
    elif pnl < 0:
        _stats["total_loss"] += -pnl
    if duration_sec is not None:
        _stats["total_duration"] += float(duration_sec)
    _stats["max_profit"] = max(float(_stats.get("max_profit", 0.0)), pnl)
    _stats["max_loss"] = min(float(_stats.get("max_loss", 0.0)), pnl)
    outcome = "WIN" if pnl > 0 else "LOSS"
    if _stats.get("current_streak_type") == outcome:
        _stats["current_streak_count"] = int(_stats.get("current_streak_count") or 0) + 1
    else:
        _stats["current_streak_type"] = outcome
        _stats["current_streak_count"] = 1
    if outcome == "WIN":
        _stats["best_streak"] = max(int(_stats.get("best_streak") or 0), int(_stats["current_streak_count"]))
    leg_strike = active.get("strike")
    leg_type = (active.get("type") or "").upper() or None
    sk = active.get("strike_key")
    # Human-readable leg: always prefer explicit strike key + CE/PE so UI never misses side.
    if sk and leg_type in ("CE", "PE"):
        leg_display = f"{symbol} {sk} {leg_type}"
    elif leg_strike:
        leg_display = f"{symbol} {leg_strike}"
        if leg_type in ("CE", "PE") and leg_type not in str(leg_strike).upper():
            leg_display = f"{leg_display} {leg_type}"
    elif leg_type in ("CE", "PE"):
        leg_display = f"{symbol} {leg_type}"
    else:
        leg_display = symbol
    global _last_trade
    _last_trade = {
        "symbol": symbol,
        "strike": leg_strike,
        "strike_key": sk,
        "type": leg_type,
        "side": leg_type,
        "leg": leg_display,
        "result": result,
        "pnl": pnl,
        "entry": entry_px,
        "exit": exit_px,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "duration_sec": duration_sec,
    }
    _append_closed(
        {
            "symbol": symbol,
            "strike": leg_strike,
            "strike_key": sk,
            "type": leg_type,
            "side": leg_type,
            "leg": leg_display,
            "entry": entry_px,
            "exit": exit_px,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "duration_sec": duration_sec,
            "pnl": pnl,
            "result": result,
        }
    )
    _last_exit_ts[symbol] = time.time()
    del _active[symbol]


def _sync_dashboard(state: Dict[str, Any]) -> None:
    active_out: Dict[str, Any] = {}
    for sym, ac in _active.items():
        entry_v = ac.get("entry")
        tgt_v = ac.get("target")
        sl_v = ac.get("stoploss")
        cur = ac.get("last_prem")
        pnl_pct = None
        dist_tgt = None
        dist_sl = None
        if cur is not None and entry_v is not None:
            try:
                e = float(entry_v)
                if e != 0:
                    pnl_pct = ((float(cur) - e) / e) * 100.0
            except (TypeError, ValueError):
                pass
        if cur is not None and tgt_v is not None:
            try:
                dist_tgt = float(tgt_v) - float(cur)
            except (TypeError, ValueError):
                pass
        if cur is not None and sl_v is not None:
            try:
                dist_sl = float(cur) - float(sl_v)
            except (TypeError, ValueError):
                pass
        active_out[sym] = {
            "entry": entry_v,
            "target": tgt_v,
            "stoploss": sl_v,
            "trailing_active": ac.get("trailing_active"),
            "type": ac.get("type"),
            "strike": ac.get("strike"),
            "index_price": ac.get("index_price"),
            "quantity": ac.get("quantity"),
            "entry_time": ac.get("entry_time"),
            "pnl_live": ac.get("pnl_live"),
            "last_prem": cur,
            "pnl_percent": pnl_pct,
            "distance_to_target": dist_tgt,
            "distance_to_sl": dist_sl,
        }
    state["paper_trades"] = {
        "active": active_out,
        "stats": _build_dashboard_stats(),
        "insights": _build_dashboard_insights(),
        "last_trade": _last_trade,
        "guide": _DASHBOARD_GUIDE,
    }


def process_tick(
    symbol: str,
    final_fast: Dict[str, Any],
    chain_snapshot: Any,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    """
    O(1) per tick: one open paper position per symbol; trailing stop; optional re-entry gate.
    """
    try:
        spot_raw = final_fast.get("price")
        try:
            spot = float(spot_raw) if spot_raw is not None else None
        except (TypeError, ValueError):
            spot = None

        active = _active.get(symbol)
        if active is not None:
            if spot is not None:
                active["index_price"] = spot
            strike_key = active.get("strike_key")
            option_type = active.get("type")
            row = chain_snapshot.get(strike_key, {}) if isinstance(chain_snapshot, dict) else {}
            leg = row.get(option_type) if isinstance(row, dict) else None
            if not isinstance(leg, dict) or leg.get("ltp") is None:
                if state is not None:
                    _sync_dashboard(state)
                return
            try:
                prem = float(leg.get("ltp"))
            except (TypeError, ValueError):
                if state is not None:
                    _sync_dashboard(state)
                return

            # Sanity guard for mismatched/invalid option premium ticks.
            last_prem = active.get("last_prem")
            if last_prem is not None:
                try:
                    last_prem_f = float(last_prem)
                    if last_prem_f > 0:
                        drop_pct = abs(prem - last_prem_f) / last_prem_f
                        if drop_pct > 0.8:
                            prem = last_prem_f
                    if prem < 5:
                        prem = last_prem_f
                except (TypeError, ValueError):
                    pass
            entry_px = float(active["entry"])
            tgt = float(active["target"])
            qty = int(active["quantity"])
            hard_sl = float(active.get("hard_sl") or (entry_px * 0.85))
            # Early panic-exit if option premium drops >5% in one tick.
            rapid_drop = False
            if last_prem is not None:
                try:
                    last_prem_f = float(last_prem)
                    if last_prem_f > 0 and ((last_prem_f - prem) / last_prem_f) > 0.05:
                        rapid_drop = True
                except (TypeError, ValueError):
                    pass
            active["last_prem"] = prem
            active["pnl_live"] = (prem - entry_px) * qty
            # Track movement timestamp for early stale-exit checks.
            try:
                move_eps = max(0.2, entry_px * 0.002)
            except Exception:
                move_eps = 0.2
            if last_prem is None or abs(float(prem) - float(last_prem)) >= move_eps:
                active["last_move_ts"] = time.time()

            # 2-stage trailing:
            #   Stage-1: at +20%, move SL to breakeven.
            #   Stage-2: near target zone, trail at 90% of live premium.
            if prem >= entry_px * 1.2:
                active["trailing_active"] = True
                active["stoploss"] = max(float(active["stoploss"]), entry_px)
            if tgt > 0 and prem >= tgt * 0.7:
                active["trailing_active"] = True
                active["stoploss"] = max(float(active["stoploss"]), prem * 0.9)
            # Simulated partial booking marker.
            if not bool(active.get("half_booked")) and prem >= entry_px * 1.25:
                active["half_booked"] = True

            sl = float(active["stoploss"])
            if prem >= tgt:
                _close_trade(symbol, active, prem, "FULL_TARGET")
            elif rapid_drop:
                _close_trade(symbol, active, prem, "LOSS")
            elif prem <= min(sl, hard_sl):
                res = "TRAIL_EXIT" if active.get("trailing_active") else "LOSS"
                _close_trade(symbol, active, prem, res)
            elif (time.time() - float(active.get("last_move_ts") or time.time())) >= 20.0:
                _close_trade(symbol, active, prem, "TRAIL_EXIT")

            if symbol in _active:
                if state is not None:
                    _sync_dashboard(state)
                return

        # Flat
        ed = final_fast.get("entry_decision")
        strike_label = final_fast.get("strike")
        sk, side = _strike_key_side(strike_label)
        prem_flat: Optional[float] = None
        if sk and side in ("CE", "PE"):
            prem_flat = _ltp(chain_snapshot, sk, side)

        if ed not in ("BUY_CE", "BUY_PE"):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        # Cooldown after exit to avoid immediate churn.
        if (time.time() - float(_last_exit_ts.get(symbol) or 0.0)) < 10.0:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        # Max 6 entries/hour per symbol.
        now_ts = time.time()
        ts_list = _entry_ts.setdefault(symbol, [])
        while ts_list and (now_ts - ts_list[0]) > 3600.0:
            ts_list.pop(0)
        if len(ts_list) >= 6:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        entry = final_fast.get("entry")
        target = final_fast.get("target")
        stoploss = final_fast.get("stoploss")
        if entry is None or target is None or stoploss is None:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        if not sk or side not in ("CE", "PE"):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        if (ed == "BUY_CE" and side != "CE") or (ed == "BUY_PE" and side != "PE"):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        try:
            entry_f = float(entry)
            tgt_f = float(target)
            sl_f = float(stoploss)
        except (TypeError, ValueError):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        if entry_f <= 0:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return

        if not _reentry_ok(symbol, ed, spot, prem_flat):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return

        # Entry filters: keep O(1) using only current + latest flat history values.
        if prem_flat is None or prem_flat < 30 or prem_flat > 300:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        try:
            strike_f = float(sk)
        except (TypeError, ValueError):
            strike_f = None
        if strike_f is None or spot is None or abs(strike_f - float(spot)) > 150:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        # Safety: skip low-liquidity or high-spread entries.
        row = chain_snapshot.get(sk, {}) if isinstance(chain_snapshot, dict) else {}
        leg = row.get(side) if isinstance(row, dict) else None
        if not isinstance(leg, dict):
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        vol_now = float(leg.get("volume") or 0.0)
        if vol_now <= 0:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        try:
            bid = float(leg.get("bid_price")) if leg.get("bid_price") is not None else None
            ask = float(leg.get("ask_price")) if leg.get("ask_price") is not None else None
        except (TypeError, ValueError):
            bid, ask = None, None
        if bid is not None and ask is not None and ask > 0 and ((ask - bid) / ask) > 0.03:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        hist = _flat_hist.get(symbol) or {}
        prev_spot = (hist.get("spot") or (None, None))[1]
        prev_prem = (hist.get("prem") or (None, None))[1]
        # Trade scoring gate: confidence + momentum + volume.
        conf_now = float(final_fast.get("confidence") or 0.0)
        if prev_spot is not None and spot is not None and float(prev_spot) != 0:
            mom_component = abs((float(spot) - float(prev_spot)) / float(prev_spot)) * 1000.0
        else:
            mom_component = 0.0
        vol_component = min(20.0, vol_now / 5000.0)
        trade_score = conf_now + mom_component + vol_component
        if trade_score < 72.0:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        # Pullback entry filter: enter only on >=2% premium pullback.
        if prev_prem is not None and prem_flat is not None and float(prem_flat) >= float(prev_prem) * 0.98:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        if prev_prem is not None and abs(float(prem_flat) - float(prev_prem)) < 5:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return
        if prev_spot is not None and spot is not None and abs(float(spot) - float(prev_spot)) < 8:
            _update_flat_hist(symbol, spot, prem_flat)
            if state is not None:
                _sync_dashboard(state)
            return

        lot = _LOT_SIZE_BY_SYMBOL.get(symbol.upper(), _LOT_SIZE_BY_SYMBOL["NIFTY"])
        lots = _lots_for_confidence(float(final_fast.get("confidence") or 0.0))
        qty = int(lot * lots)
        # Dynamic target by trend strength.
        strong_trend = mom_component >= 0.8
        tgt_f = float(entry_f * (1.6 if strong_trend else 1.3))
        _active[symbol] = {
            "entry": entry_f,
            "target": tgt_f,
            "stoploss": sl_f,
            "hard_sl": entry_f * 0.85,
            "trailing_active": False,
            "half_booked": False,
            "last_move_ts": time.time(),
            "type": side,
            "strike": strike_label if isinstance(strike_label, str) else f"{sk} {side}",
            "strike_key": sk,
            "quantity": qty,
            "lots": lots,
            # Optional scaling metadata (split lots model, still O(1) and single managed position).
            "entries": [
                {"qty": qty // 2, "entry": entry_f},
                {"qty": qty - (qty // 2), "entry": entry_f},
            ],
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "pnl_live": None,
            "last_prem": None,
        }
        ts_list.append(now_ts)
        _update_flat_hist(symbol, spot, prem_flat)
        if state is not None:
            _sync_dashboard(state)
    except Exception:
        if state is not None:
            try:
                _sync_dashboard(state)
            except Exception:
                pass
