# Project summary — `feature/multi-engine-platform`

**Branch:** `feature/multi-engine-platform`  
**HEAD (short):** `672a3c7`  
**Purpose:** Live NIFTY/SENSEX scalping signals with a **multi-engine platform** (per-engine outputs → aggregate → risk), FastAPI + React dashboard, Angel WebSocket feed, SQLite history, and optional paper-trading hooks.

---

## How you run it

Entry point starts **uvicorn** on `0.0.0.0` using `PORT` or `settings.port` (default in settings is often `8000`; your setup commonly uses `8001`).

```11:13:d:\Angel Trading\free_quant_scalping_ai\main.py
if __name__ == "__main__":
    port = int(os.environ.get("PORT", settings.port))
    uvicorn.run("api.api_server:app", host="0.0.0.0", port=port, reload=False)
```

---

## High-level architecture

1. **Angel WS** (`data/angel_ws_manager.py`) maintains index LTP + per-strike CE/PE legs in memory.  
2. **`app/services/websocket_service.py`** exposes a thin API for the live path (price, chain copy, feed health).  
3. **`api/api_server.py`** runs a **background tick loop**: for each index symbol it copies chain snapshot, runs **`run_engine_tick`**, builds `final_fast` + `signals[symbol]`, optional DB/file logging, and **`paper_trader.process_tick`**.  
4. **`engines/platform_runner.py`** is the **multi-engine tick**: scalping pipeline → `MarketState` → default engines + regime + support/resistance → **aggregate** → **risk**.  
5. **React dashboard** (`dashboard/react_dashboard/`) polls `/signals`, `/engines`, etc., and separates **fast scalping** vs **platform aggregate** in the scalping card.

---

## Multi-engine platform (core of this branch)

Each tick runs the scalping pipeline once, then runs all registered engines, then aggregates CE vs PE engine votes with regime-aware weights and conflict handling.

```15:107:d:\Angel Trading\free_quant_scalping_ai\engines\platform_runner.py
def run_engine_tick(
    global_state: Dict[str, Any],
    *,
    symbol: str,
    price: float,
    chain_snapshot: Dict[str, Dict[str, Dict[str, Any]]],
    sym_hist: List[float],
    oi_snap: Dict[str, Any],
    engines: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    One tick: run scalping pipeline once, build MarketState, run all engines, aggregate, risk.
    Mutates global_state decision + side_balance the same way as inline api path.
    """
    ...
    sp_result = run_scalping_pipeline(...)
    ms = MarketState(...)
    eng_list = engines if engines is not None else default_engines()
    engine_outputs: Dict[str, Dict[str, Any]] = {}
    for eng in eng_list:
        engine_outputs[eng.name] = eng.process_tick(ms)
    ...
    agg = aggregate(
        engine_outputs,
        regime=regime,
        support_resistance=sr_output.get("metadata") or {},
    )
    risk_report, agg_after_risk = evaluate_risk(global_state, symbol, ms, agg)

    return {
        "scalping_pipeline": sp_result,
        "regime": regime,
        "engines": engine_outputs,
        "aggregate": agg_after_risk,
        "aggregate_raw": agg,
        "risk": risk_report,
        "support_resistance": sr_output,
        "market_state_ts": now_sec,
    }
```

The aggregator returns a **single platform signal** plus confidence, intent, and metadata (engine lists, scores, conflict flag).

```127:171:d:\Angel Trading\free_quant_scalping_ai\engines\aggregator.py
    conflict = bool(ce_names and pe_names)
    winner, win_score, resolve_reason = _conflict_resolution(ce_score, pe_score, regime_name, support_resistance, engine_outputs)
    ...
    return {
        "signal": winner,
        "confidence": round(base_conf, 2),
        "intent": final_intent,
        "reason": "|".join(reason_parts),
        "metadata": {
            "regime": reg,
            "weights": weights,
            "normalized_confidence": normalized,
            "ce_score": round(ce_score, 4),
            "pe_score": round(pe_score, 4),
            "ce_engines": ce_names,
            "pe_engines": pe_names,
            "agreement_engines": agree_names,
            "agreement_ratio": round(agreement_ratio, 4),
            "conflict": conflict,
            "support_resistance": support_resistance or {},
        },
    }
```

---

## Live API path (fast signals)

The heavy per-tick work runs in **`_compute_for_symbol_impl`** (sync). The async wrapper **`compute_for_symbol`** schedules it on the default **thread pool executor** so HTTP stays responsive. The main loop sleep is configurable via **`LIVE_TICK_SLEEP_SEC`** (default `0.25`).

```187:296:d:\Angel Trading\free_quant_scalping_ai\api\api_server.py
def _compute_for_symbol_impl(symbol: str) -> None:
    """
    High-performance live path (runs in a thread pool so FastAPI's event loop stays free):
      1) read latest Angel live price
      2) read fast in-memory option chain snapshot (no pandas)
      3) compute fast features
      4) generate scalping + hero-zero signals
      5) compute lightweight confidence
      6) update fast state and (optionally) persist
    """
    su = symbol.upper()

    price = get_index_ltp(su)
    chain_snapshot = get_option_chain_snapshot(su)
    ...
    oi_snap = _quick_oi(chain_snapshot)
    platform = run_engine_tick(
        state,
        symbol=symbol,
        price=float(price or 0.0),
        chain_snapshot=chain_snapshot,
        sym_hist=sym_hist,
        oi_snap=oi_snap,
    )
    sp = platform["scalping_pipeline"]
    state.setdefault("engine_platform", {})[symbol] = {
        "engines": platform["engines"],
        "aggregate": platform["aggregate"],
        "aggregate_raw": platform.get("aggregate_raw"),
        "risk": platform["risk"],
        "regime": platform.get("regime"),
        "support_resistance": platform.get("support_resistance"),
    }
```

```549:574:d:\Angel Trading\free_quant_scalping_ai\api\api_server.py
async def compute_for_symbol(symbol: str) -> None:
    """Schedule one tick on the default executor; keeps HTTP/WebSocket responsive during heavy work."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _compute_for_symbol_impl, symbol)

def _live_tick_sleep_sec() -> float:
    try:
        s = float(os.getenv("LIVE_TICK_SLEEP_SEC", "0.25"))
    except ValueError:
        s = 0.25
    return max(0.05, min(5.0, s))

async def main_loop():
    sleep_sec = _live_tick_sleep_sec()
    logger.info("[LIVE] main loop tick interval=%.2fs (set LIVE_TICK_SLEEP_SEC to override)", sleep_sec)
    while True:
        for symbol in settings.indices:
            try:
                await compute_for_symbol(symbol)
            except Exception as exc:
                ...
        await asyncio.sleep(sleep_sec)
```

---

## WebSocket facade + option chain

The API server avoids importing Angel internals directly; it calls **`websocket_service`**.

```12:22:d:\Angel Trading\free_quant_scalping_ai\app\services\websocket_service.py
def get_index_ltp(symbol: str) -> Optional[float]:
    return get_ws_price(symbol)


def get_option_chain_snapshot(symbol: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    return get_option_chain_copy(symbol)


def get_feed_health_snapshot() -> Dict[str, Any]:
    return get_feed_health()
```

Option legs written by the WS manager include a monotonic **`ts`** (epoch seconds) on each leg update so the API can compute **LTP age** when building snapshots.

```204:217:d:\Angel Trading\free_quant_scalping_ai\data\angel_ws_manager.py
                    sk = str(int(strike)) if strike == int(strike) else str(strike)
                    option_chain_cache.setdefault(under, {})
                    option_chain_cache[under].setdefault(sk, {})
                    coi = oi_change if oi_change is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("change_oi")
                    leg = {
                        "ltp": ltp,
                        "oi": float(oi) if oi is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("oi"),
                        "volume": int(vol) if vol is not None else option_chain_cache.get(under, {}).get(sk, {}).get(opt_type, {}).get("volume", 0) or 0,
                        "change_oi": coi,
                        "oi_change": coi,
                        "token": token,
                        "ts": now,
                    }
                    option_chain_cache[under][sk][opt_type] = leg
```

---

## Strike selection (fast + consensus legs)

`option_chain_service` resolves messy strike keys, reads LTP from multiple possible field names, and selects an ATM-relative strike using premium band + liquidity, tunable via env vars (`STRIKE_PREMIUM_MIN`, `STRIKE_PREMIUM_MAX`, `STRIKE_ATM_STEPS`).

```11:90:d:\Angel Trading\free_quant_scalping_ai\app\services\option_chain_service.py
def chain_row_for_strike(chain: Dict[str, Any], s_val: float) -> Dict[str, Any]:
    """Resolve option row dict for a strike; keys vary by feed (int string, float string, etc.)."""
    ...


def option_leg_last_price(leg: Any) -> Optional[float]:
    """Best-effort option LTP from WS / REST snapshots (keys differ by source)."""
    ...


def select_strike_for_scalp(
    chain: Dict[str, Dict[str, Dict[str, Any]]],
    ref_price: float,
    trade: str,
) -> Optional[Dict[str, Any]]:
    """
    Pick best strike near ATM for CE/PE using volume+OI score.
    Premium band is env-tunable (defaults widened vs old 80–250).
    """
    ...
    prem_min = _env_float("STRIKE_PREMIUM_MIN", 30.0)
    prem_max = _env_float("STRIKE_PREMIUM_MAX", 450.0)
    max_steps = int(_env_float("STRIKE_ATM_STEPS", 8.0))
```

---

## Persistence & offline analysis

- **SQLite** (`storage/market_data.sqlite`): `store_signal(..., category="final_fast", ...)` for throttled history used by `/signal-history`.  
- **JSONL** (`logs/signal_events.jsonl`): higher-frequency compact rows for auditing.  
- **Reporter:** `analysis/yesterday_signals_report.py` — pass a `YYYY-MM-DD` date to summarize counts/reasons for that UTC day prefix.

---

## Dashboard (React)

The scalping section intentionally separates:

- **Fast scalping** headline + first grid (driven by stabilizer / `NO_TRADE` vs `BUY_*`).  
- **Consensus leg** (built from **platform aggregate** + live chain) when aggregate is directional.  
- Split polling: fast endpoints vs slow/heavy endpoints (env-tunable `VITE_POLL_FAST_MS`, `VITE_POLL_SLOW_MS`).

Implementation lives primarily in `dashboard/react_dashboard/src/App.jsx` and `api.js`.

---

## Operational notes (this branch)

- **Angel credentials** must be present in the environment for WS + live ticks; otherwise you will see `waiting_index_price` / empty chain coverage.  
- **Port `8001` conflicts** (`WinError 10048`) mean a previous uvicorn is still bound — stop the old PID before restarting.  
- **Env knobs:** `LIVE_TICK_SLEEP_SEC`, `WS_FAST_PUSH_SEC`, `WS_SIGNALS_PUSH_SEC`, strike premium band vars above, feed staleness `MAX_INDEX_TICK_AGE_SEC`.

---

## Key file map

| Area | Path |
|------|------|
| API + live loop | `api/api_server.py` |
| Engine orchestration | `engines/platform_runner.py` |
| Aggregation | `engines/aggregator.py` |
| Scalping pipeline | `engines/scalping_pipeline.py` |
| WS + chain cache | `data/angel_ws_manager.py` |
| WS facade | `app/services/websocket_service.py` |
| Strike / LTP helpers | `app/services/option_chain_service.py` |
| SQLite | `data/data_storage.py` |
| Paper sim | `paper_trader.py` |
| UI | `dashboard/react_dashboard/src/App.jsx` |
| Day report | `analysis/yesterday_signals_report.py` |

This document is generated to match the **current branch** and the cited line ranges in the working tree at summary time.
