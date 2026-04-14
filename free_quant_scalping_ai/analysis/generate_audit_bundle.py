#!/usr/bin/env python3
"""
Offline audit bundle: parse local JSONL logs + emit structured JSON for analysis.

Run from repo root:
  python analysis/generate_audit_bundle.py
  python analysis/generate_audit_bundle.py --signals logs/signal_events.jsonl --tail 20000

Outputs under analysis/audit_export/ (overwritten each run).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "analysis" / "audit_export"

# Curated env keys referenced by signal / execution / risk paths (defaults in comments are code defaults).
CONFIG_KEYS = [
    "MIN_CONF_NIFTY",
    "MIN_CONF_SENSEX",
    "STABLE_CYCLES_NIFTY",
    "STABLE_CYCLES_SENSEX",
    "ENTRY_LOCK_SEC_NIFTY",
    "ENTRY_LOCK_SEC_SENSEX",
    "SIGNAL_HOLD_SEC_NIFTY",
    "SIGNAL_HOLD_SEC_SENSEX",
    "HIGH_CONF_UNLOCK_NIFTY",
    "HIGH_CONF_UNLOCK_SENSEX",
    "LOCK_PRICE_MOVE_NIFTY",
    "LOCK_PRICE_MOVE_SENSEX",
    "TREND_BLOCK_POINTS_NIFTY",
    "TREND_BLOCK_POINTS_SENSEX",
    "MIN_RANGE_FRAC_NIFTY",
    "MIN_RANGE_FRAC_SENSEX",
    "SIDE_BALANCE_SKEW",
    "SIDE_BALANCE_EDGE_GAP",
    "BIAS_TIEBREAK_MIN_CONF",
    "BIAS_TIEBREAK_MIN_GAP",
    "ML_CONFIDENCE_FLOOR",
    "AGGREGATE_MARGIN",
    "AGGREGATE_AGREE_BONUS",
    "AGGREGATE_CONFLICT_PENALTY",
    "AGGREGATE_VOLATILE_PENALTY",
    "ENGINE_WEIGHT_HERO_ZERO",
    "ENGINE_WEIGHT_SCALPING",
    "ENGINE_WEIGHT_HOLD",
    "ENGINE_WEIGHT_CALL_SIDE",
    "ENGINE_WEIGHT_PUT_SIDE",
    "ENGINE_WEIGHT_SUPPORT_RESISTANCE",
    "RISK_MAX_RANGE_FRAC",
    "RISK_MIN_STRIKES",
    "RISK_MIN_OPTION_LEGS",
    "RISK_COOLDOWN_SEC",
    "RISK_MAX_SIGNALS_PER_MIN",
    "EXEC_MIN_ENTRY_CONFIDENCE",
    "EXEC_REENTRY_MIN_UNDERLYING_MOVE",
    "ENTRY_MAX_LTP_AGE_SEC",
    "EXEC_MIN_PRICE_MOVE",
    "EXEC_NO_TRADE_RANGE_LOOKBACK",
    "EXEC_NO_TRADE_MAX_INDEX_RANGE",
    "EXEC_USE_ATM_WINDOW",
    "EXEC_ATM_STEPS_BACK",
    "EXEC_ATM_STEPS_FWD",
    "STRIKE_PREMIUM_MAX",
    "EXEC_MAX_SPREAD_PCT",
    "SYSTEM_WARMUP_SEC",
    "ENTRY_MIN_TICKS",
    "RESTART_GUARD_SEC",
    "RESTART_CONF_DELTA",
    "LIVE_TICK_SLEEP_SEC",
    "MAX_INDEX_TICK_AGE_SEC",
    "SIGNAL_RECORD_EXTENDED",
    "AUDIT_LATENCY_JSONL",
    "SIGNAL_RECORD_FILE",
    "PAPER_TRADES_LOG",
]


def pipeline_logic_reference() -> Dict[str, Any]:
    """STEP 1: code-level reference (summaries, not raw source)."""
    return {
        "scalping_pipeline": {
            "module": "engines/scalping_pipeline.py",
            "flow": [
                "compute_fast_features(chain, price, sym_hist)",
                "generate_fast_scalping_signal(features, symbol)  # scalping_fast",
                "generate_ml_signal(features, symbol)",
                "compute_fast_confidence(features)",
                "merge_ml_fallback (if rule NO_TRADE and ML label directional + ML_CONFIDENCE_FLOOR)",
                "sensex_premium_fallback (SENSEX PE/CE ratio + momentum)",
                "bias_tiebreak (OI/vol/mom scoring when still NO_TRADE)",
                "apply_trend_filter (block CE if downtrend, PE if uptrend vs TREND_BLOCK_POINTS_*)",
                "apply_volatility_filter (MIN_RANGE_FRAC_* on last 20 closes)",
                "apply_side_balance_filter (skew + edge gap decay)",
                "apply_hysteresis (hold last directional up to SIGNAL_HOLD_SEC_*)",
                "apply_entry_stabilizer → entry_decision, decision_reason, stable_count, lock",
            ],
            "rule_thresholds_fast": {
                "source": "scalping_fast.generate_fast_scalping_signal",
                "CALL_THRESH": 0.38,
                "PUT_THRESH": 0.38,
                "VOL_THRESH": 0.42,
                "MOM_UP": 8e-5,
                "MOM_DOWN": -8e-5,
                "SENSEX_no_volume": "OI change / spread_skew substitutes with CHG_THRESH=0.30, SPREAD_CE/PE, OI_RELAX=0.30",
                "conflict_both_sides": "NO_TRADE",
            },
            "confidence_formula": "compute_fast_confidence: 0.4*max(oi)+0.3*max(vol)+0.3*mom_norm → map to [50,95]; SENSEX volume penalty SENSEX_VOLUME_CONF_PENALTY",
        },
        "entry_stabilizer": {
            "module": "app/services/signal_engine.py",
            "params_env": "MIN_CONF_NIFTY/SENSEX, STABLE_CYCLES_*, ENTRY_LOCK_SEC_*, LOCK_PRICE_MOVE_*, HIGH_CONF_UNLOCK_*",
            "logic_summary": "Requires repeated same directional scalping_signal for STABLE_CYCLES; min confidence MIN_CONF; sets entry lock for ENTRY_LOCK_SEC; during lock allows continuation same leg, or re-entry on price move >= LOCK_PRICE_MOVE or high confidence unlock.",
        },
        "multi_engine_platform": {
            "runner": "engines/platform_runner.run_engine_tick",
            "engines_default": [
                "scalping → mirrors pipeline scalping_signal/confidence",
                "hold",
                "call_side",
                "put_side",
                "hero_zero",
            ],
            "support_resistance": "SupportResistanceEngine (extra engine output, not in PRIORITY list name but SR metadata feeds aggregator)",
        },
        "aggregation": {
            "module": "engines/aggregator.py",
            "ce_pe_scoring": "Weighted sum per engine: contrib = weight * normalized_conf/100; BUY_CE adds to ce_score, BUY_PE to pe_score",
            "normalize_confidence": "Per-engine regime scaling (hold/hero_zero/scalping/call_side/put_side)",
            "regime_weights": "TRENDING/RANGING/EXPIRY_HIGH_GAMMA adjust ENGINE_WEIGHT_* base",
            "conflict_resolution": "If ce_score > pe_score * AGGREGATE_MARGIN (default 1.08) → BUY_CE; elif PE wins margin → BUY_PE; else smart tie-break using regime + support_resistance distances + PRIORITY order",
            "final_confidence": "40 + win_score*22, bonuses AGGREGATE_AGREE_BONUS, penalties AGGREGATE_CONFLICT_PENALTY / AGGREGATE_VOLATILE_PENALTY",
        },
        "risk_gating": {
            "module": "engines/risk_engine.py",
            "blocks_directional_aggregate": [
                "20-tick range / price > RISK_MAX_RANGE_FRAC",
                "strikes < RISK_MIN_STRIKES",
                "option legs < RISK_MIN_OPTION_LEGS",
                "RISK_MAX_SIGNALS_PER_MIN in60s window",
                "RISK_COOLDOWN_SEC since last directional emit",
            ],
            "effect": "Forces aggregate signal NO_TRADE and caps confidence at 40",
        },
        "strike_selection": {
            "module": "app/services/option_chain_service.py",
            "paths": [
                "select_strike_atm_window (EXEC_USE_ATM_WINDOW) — ATM±steps, liquidity + optional LTP delta vs prev tick",
                "select_strike_for_scalp — wider ATM band STRIKE_ATM_STEPS, premium band STRIKE_PREMIUM_MIN/MAX, EXEC_MAX_SPREAD_PCT",
            ],
            "target_stop_in_api": "api_server._leg_levels_for_bias: target = entry*1.4, stop = entry*0.8 (premium multiples)",
        },
        "execution_lifecycle_entry": {
            "module": "engines/trade_lifecycle.py",
            "pre_open_checks_summary": [
                "status IDLE or CLOSED",
                "entry_decision in BUY_CE/BUY_PE",
                "confidence >= EXEC_MIN_ENTRY_CONFIDENCE (+ SL_REENTRY bump after SL_OR_TRAIL exit)",
                "strike_num, opt_type, entry, target, stoploss present",
                "chain_ltp_age_sec <= ENTRY_MAX_LTP_AGE_SEC when provided",
                "CLOSED re-entry: underlying moved EXEC_REENTRY_MIN_UNDERLYING_MOVE from last_exit_underlying; same last_exit_signal as want",
                "skip_entry_reason from api_server merges runtime_stability (warmup/min_ticks/post_restart) + chop/momentum (EXEC_*) — if set → NO_TRADE emission, no OPEN",
            ],
        },
        "execution_vs_scalp": "Execution uses scalping_signal for leg selection and entry_decision for intent; aggregate_signal used for reversal logic while OPEN.",
    }


def read_jsonl(path: Path, tail: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if tail is not None and tail > 0:
        lines = lines[-tail:]
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_signals_debug(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "timestamp": r.get("ts"),
                "symbol": r.get("symbol"),
                "scalping_signal": r.get("signal"),
                "aggregate_signal": r.get("audit_aggregate_signal"),
                "aggregate_confidence": r.get("audit_aggregate_confidence"),
                "confidence": r.get("confidence"),
                "entry_decision": r.get("entry_decision"),
                "reason": r.get("decision_reason"),
                "index_price": r.get("price"),
                "risk": r.get("audit_risk"),
                "regime": r.get("audit_regime"),
                "skip_entry_reason": r.get("audit_skip_entry_reason"),
            }
        )
    return out


def build_rejected(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rows where a live entry was not taken (HOLD or NO_TRADE entry_decision)."""
    rejected: List[Dict[str, Any]] = []
    hist: Counter[str] = Counter()
    directional_blocked: List[Dict[str, Any]] = []
    for r in rows:
        ed = str(r.get("entry_decision") or "").upper()
        sig = str(r.get("signal") or "").upper()
        reason = str(r.get("decision_reason") or "")
        if ed not in ("BUY_CE", "BUY_PE"):
            hist[reason or "unknown"] += 1
            if len(rejected) < 4000:
                rejected.append(
                    {
                        "timestamp": r.get("ts"),
                        "symbol": r.get("symbol"),
                        "scalping_signal": r.get("signal"),
                        "entry_decision": r.get("entry_decision"),
                        "decision_reason": reason,
                        "confidence": r.get("confidence"),
                        "price": r.get("price"),
                        "skip_entry_reason": r.get("audit_skip_entry_reason"),
                        "aggregate_signal": r.get("audit_aggregate_signal"),
                        "risk": r.get("audit_risk"),
                    }
                )
        if sig in ("BUY_CE", "BUY_PE") and ed not in ("BUY_CE", "BUY_PE"):
            if len(directional_blocked) < 10000:
                directional_blocked.append(
                    {
                        "timestamp": r.get("ts"),
                        "symbol": r.get("symbol"),
                        "scalping_signal": sig,
                        "entry_decision": ed,
                        "decision_reason": reason,
                        "confidence": r.get("confidence"),
                    }
                )
    return {
        "summary": {
            "total_rows": len(rows),
            "rejected_entry_decision_histogram": dict(hist.most_common(40)),
            "directional_scalp_but_not_entry_decision_count": len(directional_blocked),
        },
        "directional_blocked_sample": directional_blocked[-500:],
        "rejected_sample": rejected[-1500:],
    }


def build_execution_trades(paper_log: Path) -> List[Dict[str, Any]]:
    rows = read_jsonl(paper_log)
    trades: List[Dict[str, Any]] = []
    open_by_id: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ev = r.get("event")
        tid = str(r.get("trade_id") or "")
        if ev == "open" and tid:
            open_by_id[tid] = dict(r)
        elif ev == "close" and tid:
            o = open_by_id.pop(tid, {})
            entry_t = o.get("entry_time") or o.get("ts")
            exit_t = r.get("exit_time") or r.get("ts")
            dur = r.get("trade_duration_sec") or r.get("trade_age_sec")
            trades.append(
                {
                    "trade_id": tid,
                    "symbol": r.get("symbol"),
                    "signal": r.get("signal"),
                    "entry_time": entry_t,
                    "exit_time": exit_t,
                    "entry_price": r.get("entry"),
                    "exit_price": r.get("exit"),
                    "pnl": r.get("pnl"),
                    "exit_reason": r.get("reason"),
                    "duration_sec": dur,
                }
            )
    return trades


def latency_stats(lat_path: Path) -> Dict[str, Any]:
    rows = read_jsonl(lat_path)
    if not rows:
        return {
            "note": "No latency log. Set AUDIT_LATENCY_JSONL=1 and restart API to populate logs/audit_latency.jsonl",
            "rows": 0,
        }
    ms = [float(r["compute_ms"]) for r in rows if r.get("compute_ms") is not None]
    issues = [r for r in rows if float(r.get("compute_ms") or 0) > 200]
    return {
        "rows": len(rows),
        "compute_ms_avg": round(statistics.mean(ms), 3) if ms else None,
        "compute_ms_max": round(max(ms), 3) if ms else None,
        "compute_ms_p95": round(sorted(ms)[min(len(ms) - 1, int(len(ms) * 0.95))], 3) if len(ms) > 1 else None,
        "ticks_over_200ms": len(issues),
        "run_in_executor_note": "compute_for_symbol still uses asyncio.run_in_executor(None, _compute_for_symbol_impl); heavy work off event loop.",
        "sample_tail": rows[-20:],
    }


def config_effective() -> Dict[str, Any]:
    return {k: os.environ.get(k) for k in CONFIG_KEYS}


def unused_code_hints() -> Dict[str, Any]:
    return {
        "likely_unused_packages": [
            "strategies/*.py — no imports from strategies.* found in .py tree (parallel research code)",
            "strategies/scalping_engine.py — duplicate name vs engines/scalping_engine.py; live path uses engines/",
        ],
        "duplicate_signal_paths": [
            "Legacy scalping_fast + signal_engine filters vs strategies/ folder (unused)",
            "hero_zero_fast.detect_hero_zero_fast in pipeline vs engines/hero_zero_engine.py for platform aggregate",
        ],
        "note": "Confirm with project owner before deleting; grep-based static hints only.",
    }


def write_csv_summary(rejected_hist: Dict[str, int], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["decision_reason", "count"])
        for k, v in sorted(rejected_hist.items(), key=lambda x: -x[1]):
            w.writerow([k, v])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", type=Path, default=ROOT / "logs" / "signal_events.jsonl")
    ap.add_argument("--paper", type=Path, default=ROOT / "logs" / "paper_trades.jsonl")
    ap.add_argument("--latency", type=Path, default=ROOT / "logs" / "audit_latency.jsonl")
    ap.add_argument("--tail", type=int, default=50_000)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()

    sig_rows = read_jsonl(args.signals, args.tail)
    signals_debug = build_signals_debug(sig_rows)
    rejected = build_rejected(sig_rows)
    exec_trades = build_execution_trades(args.paper)
    lat = latency_stats(args.latency)

    bundle = {
        "manifest": {
            "generated_at_utc": generated_at,
            "repo_root": str(ROOT),
            "signals_source": str(args.signals),
            "signals_rows_used": len(sig_rows),
            "note_extended_fields": "audit_* fields appear when SIGNAL_RECORD_EXTENDED=1 on running API.",
        },
        "pipeline_logic_reference": pipeline_logic_reference(),
        "config_effective": config_effective(),
        "unused_code_hints": unused_code_hints(),
    }

    (OUT_DIR / "manifest.json").write_text(json.dumps(bundle["manifest"], indent=2), encoding="utf-8")
    (OUT_DIR / "pipeline_logic_reference.json").write_text(
        json.dumps(bundle["pipeline_logic_reference"], indent=2), encoding="utf-8"
    )
    (OUT_DIR / "config_effective.json").write_text(json.dumps(bundle["config_effective"], indent=2), encoding="utf-8")
    (OUT_DIR / "unused_code_hints.json").write_text(json.dumps(bundle["unused_code_hints"], indent=2), encoding="utf-8")
    (OUT_DIR / "signals_debug.json").write_text(json.dumps(signals_debug, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "rejected_signals.json").write_text(json.dumps(rejected, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "execution_trades.json").write_text(json.dumps(exec_trades, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "latency_metrics.json").write_text(json.dumps(lat, indent=2, default=str), encoding="utf-8")

    hist = rejected.get("summary", {}).get("rejected_entry_decision_histogram") or {}
    write_csv_summary(hist, OUT_DIR / "rejected_reasons_histogram.csv")

    print(f"Wrote audit bundle to {OUT_DIR}")


if __name__ == "__main__":
    main()
