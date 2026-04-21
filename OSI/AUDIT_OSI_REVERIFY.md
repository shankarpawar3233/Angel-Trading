# OSI Audit Pack (For ChatGPT Re-Verification)

## Scope

This document summarizes what has been implemented in `D:\Angel Trading\OSI` for the Options Signal Intelligence (OSI) system, including architecture, logic, fixes, and current runtime status.

Use this with ChatGPT to review:
- architecture correctness
- strategy/consensus logic
- signal lifecycle logic
- production-readiness risks

## Current Project Path

- Root: `D:/Angel Trading/OSI`
- Backend package: `D:/Angel Trading/OSI/osi`
- Dashboard: `D:/Angel Trading/OSI/osi/ui/dashboard.html`

## What Is Implemented

### 1) Live Backend + Dashboard

- FastAPI backend in `osi/main.py`
- REST endpoints:
  - `GET /signals`
  - `GET /history`
  - `GET /metrics`
  - `GET /dashboard` (serves HTML dashboard)
- WebSocket endpoint:
  - `WS /signals/live`

### 2) Angel One SmartWebSocketV2 Integration

Implemented in `osi/data/angel_ws.py`:
- SmartAPI login/session creation
- SmartWebSocketV2 connect and callbacks
- auto reconnect with exponential backoff + jitter
- dynamic index subscription (`NIFTY`, `SENSEX`)
- dynamic option token subscription by ATM bands
- instrument master driven contract lookup
- fallback to nearest available expiry in instrument master if preferred expiry is unavailable

### 3) Instrument Master Loader

Implemented in `osi/data/instrument_master.py`:
- downloads/loads Angel OpenAPI scrip master
- local cache file in `storage/angel_instruments.json`

### 4) Candle Builder (Engine trigger on close)

Implemented in `osi/services/candle_builder.py`:
- 1m candle
- 3m candle
- 5m candle

Engines run only on candle close via pipeline processing.

### 5) Strategy Engines (separate files)

Implemented files:
- `osi/engines/scalping_engine.py`
- `osi/engines/smc_engine.py`
- `osi/engines/hero_zero_engine.py`
- `osi/engines/trend_engine.py`
- `osi/engines/mean_reversion_engine.py`
- `osi/engines/option_chain_engine.py`
- `osi/engines/ml_engine.py`

All return standardized output:
- `engine`
- `signal` (`BUY_CE` / `BUY_PE` / `NONE`)
- `strength` (0-1)

### 6) Market Regime + Consensus

- Regime engine in `osi/engines/market_regime_engine.py`
  - detects `TRENDING` vs `SIDEWAYS`
- Consensus in `osi/engines/consensus_engine.py`
  - weighted combine across all engines
  - ml_engine weight reduced to `0.6`
  - mean reversion weight disabled in trending regime
  - scalping weight reduced in sideways regime
  - confidence cap when ML strength < 0.5

### 7) Signal Manager / Lifecycle

Implemented in `osi/services/signal_manager.py`:
- confidence threshold gate
- duplicate signal block
- cooldown per symbol
- anti flip-flop protection
- max active signals per symbol
- strike selection by lead strategy intent
- lifecycle events:
  - `ACTIVE`
  - `SL_MOVED_TO_ENTRY`
  - `SL_HIT`
  - `CLOSED` on target logic
- tracks:
  - strike
  - entry price (frozen)
  - current option LTP
  - target
  - exit price
  - pnl

### 8) Pipeline + Metrics

Implemented in `osi/services/pipeline.py` and `osi/services/metrics.py`:
- event-driven tick queue
- candle-close compute path
- per-engine execution timings
- latency metrics:
  - tick_to_process_latency
  - process_to_signal_latency
  - total_signal_latency

### 9) Dashboard

Implemented in `osi/ui/dashboard.html`:
- WebSocket status
- live metrics
- active signals table
- latest live decision
- engine outputs
- consensus JSON
- history table
- columns include strike, entry, LTP, target, exit

## Important Runtime Behavior Confirmed

Observed in logs:
- backend starts successfully
- dashboard and APIs respond
- WebSocket to frontend is connected
- SmartWebSocketV2 connection established
- index ticks are received
- option-chain token selection initially failed due to strict expiry; fixed with nearest-available-expiry fallback
- after fix, pipeline processed ticks (example observed: >7000 processed)

## Current Configuration Philosophy

- Standalone default mode (no mandatory Redis/Postgres runtime requirement)
- No external enable flags required for basic running
- credentials accepted via:
  - `OSI_ANGEL_*`
  - or fallback legacy `ANGEL_*`

## Run Command (Current)

From `D:/Angel Trading/OSI`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn osi.main:app --host 0.0.0.0 --port 8010 --reload
```

Dashboard:
- `http://localhost:8010/dashboard`

## Key Files to Review

- `osi/main.py`
- `osi/api/routes.py`
- `osi/data/angel_ws.py`
- `osi/data/instrument_master.py`
- `osi/services/candle_builder.py`
- `osi/services/pipeline.py`
- `osi/services/signal_manager.py`
- `osi/engines/consensus_engine.py`
- `osi/engines/market_regime_engine.py`
- `osi/ui/dashboard.html`

## Known Open Calibration Area

System currently computes signals correctly, but if active signals are sparse, threshold/weights may need calibration for live market behavior (confidence often below trigger when engines disagree).

## Re-Verification Questions for ChatGPT

1. Is the pipeline architecture robust enough for production (threading + async boundaries + websocket callbacks)?
2. Is dynamic option token selection logic correct for weekly expiry rotation and symbol naming differences?
3. Are candle-close semantics correctly implemented for 1m/3m/5m with minimal drift?
4. Is consensus weighting mathematically balanced for low-latency options trading?
5. Is lifecycle logic (SL move + close conditions + cooldown/flip-flop) consistent and safe?
6. Which additional risk controls should be added before live execution wiring?
7. Any critical bug risks in `angel_ws.py` parsing/subscription flow?

