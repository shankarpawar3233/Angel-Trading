# Options Signal Intelligence (OSI)

OSI is an event-driven Indian index options signal stack for NIFTY and SENSEX. It ingests Angel One SmartWebSocketV2 ticks, builds candles, runs independent strategy engines, routes outputs through time-aware consensus, manages signal lifecycle/risk, and publishes live state through FastAPI, WebSocket, Redis, and optional PostgreSQL.

## Current Features

- Live Angel One SmartWebSocketV2 ingestion with reconnect handling, index-first subscription, ATM option token selection, and dynamic CE/PE subscriptions.
- 1m, 3m, and 5m candle building from live ticks, plus tick-level intrabar evaluation.
- Independent engines for intrabar momentum, smart breakout, zero hero, and mean reversion.
- Time-based strategy routing without disabling engines:
  - `MORNING` 09:15-11:00 favors intrabar/momentum and breakout.
  - `MIDDAY` 11:00-13:30 favors mean reversion.
  - `AFTERNOON` 13:45-15:30 favors breakout continuation.
  - `OFF` is outside active routing windows.
- Phase-aware consensus weights, volume filters, and decision metadata (`market_phase`, `engine_weights`, `selected_engine`).
- Strong intrabar path can execute directly when strength is high, with pending confirmation for weaker intrabar signals.
- Signal lifecycle management: duplicate protection, tick confirmation, flip protection, cooldown, partial booking, SL trail, target exits, momentum exits, and max-hold exits.
- Risk controls: daily loss cap, consecutive SL cap, optional max trade notional cap, manual pause/resume, and risk state in `/metrics`.
- Latency observability: `latency_ms` snapshots, `LATENCY_PATH` logs, queue depth, engine execution timings, Redis intrabar throttle.
- Dashboard/API output for active, historical, rejected, paper, and strategy signals.
- Telegram notifications and commands when enabled.
- Credential redaction filter for broker SDK logs.
- Redis and PostgreSQL are optional at runtime; Redis falls back to memory and PostgreSQL persistence disables itself if unavailable.

## Project Structure

```text
osi/
  main.py                       FastAPI app startup/shutdown
  api/
    routes.py                   REST/WebSocket/dashboard routes
  core/
    config.py                   OSI_* settings
    log_config.py               logging setup and credential redaction
    market_phase.py             market phase detector and phase weights
    models.py                   Pydantic/runtime models
  data/
    angel_ws.py                 Angel SmartWebSocketV2 source and token routing
    instrument_master.py        instrument master loading/filtering
  engines/
    base.py
    consensus_engine.py         phase-weighted final signal selection
    intrabar_engine.py          tick/live-candle momentum breakout
    smart_breakout_engine.py    phase-aware breakout/volume logic
    mean_reversion_engine.py    phase-aware VWAP/z-score reversal logic
    hero_zero_engine.py
    market_regime_engine.py
    ml_engine.py
    option_chain_engine.py
    scalping_engine.py
    smc_engine.py
    trend_engine.py
  infra/
    redis_store.py              live state cache with memory fallback
    postgres_repo.py            optional signal/snapshot persistence
  services/
    candle_builder.py           1m/3m/5m candle aggregation
    expiry_engine.py
    metrics.py
    option_momentum.py
    pipeline.py                 tick worker, engines, consensus, publishing
    power_guard.py              Windows sleep prevention
    signal_manager.py           signal creation, lifecycle, storage, risk
    telegram_notifier.py
  ui/
    dashboard.html
```

## Runtime Flow

```text
Angel SmartWebSocketV2
  -> AngelWebSocketSource normalizes index/option ticks
  -> OSIPipeline.on_tick queues MarketTick
  -> CandleBuilder updates live/closed candles
  -> pending intrabar confirmation
  -> IntrabarEngine on live 1m candle context
  -> candle engines on closed candles
  -> ConsensusEngine applies market-phase weights
  -> SignalManager applies risk, confirmation, flip/cooldown, contract selection
  -> Redis live state, WebSocket fanout, optional PostgreSQL snapshots
  -> dashboard/API/Telegram consumers
```

Intrabar is tick-level. Candle engines run on closed candles. Engines remain independent; time-based behavior is applied with weights, thresholds, confidence adjustments, and soft filters rather than deleting engine outputs.

## Market Phase Routing

`osi/core/market_phase.py` owns the phase detector and weights:

```text
MORNING    09:15-11:00  intrabar 1.8, smart_breakout 1.4, mean_reversion 0.6
MIDDAY     11:00-13:30  mean_reversion 1.6, intrabar 0.8, smart_breakout 0.8
AFTERNOON  13:45-15:30  smart_breakout 1.8, intrabar 1.2, mean_reversion 0.5
OFF        otherwise    default engine weight 1.0
```

Additional routing behavior:

- `11:30-12:30` is a soft no-trade zone: final confidence is reduced by 10 and must remain at least 35.
- Midday mean reversion requires stronger reversal evidence around VWAP deviation, z-score, and candle overextension.
- Phase volume filters are stricter in midday and afternoon.
- Afternoon smart breakout uses tighter breakout confirmation, smaller SL, and faster targets.

## Run

Install dependencies:

```bash
pip install -r requirements.txt
```

Create `.env` from the sample:

```powershell
copy osi\config.sample.env .env
```

Fill Angel credentials in `.env`:

```text
OSI_ANGEL_API_KEY=...
OSI_ANGEL_CLIENT_ID=...
OSI_ANGEL_PASSWORD=...
OSI_ANGEL_TOTP_SECRET=...
OSI_ANGEL_FEED_TOKEN=
```

Start the API:

```bash
uvicorn osi.main:app --host 0.0.0.0 --port 8011 --reload
```

Open:

- API docs: <http://localhost:8011/docs>
- Dashboard: <http://localhost:8011/dashboard>

## Important Settings

All settings use the `OSI_` prefix.

- `OSI_ENABLE_SENSEX`: enable SENSEX alongside NIFTY.
- `OSI_REDIS_URL`: Redis live state cache.
- `OSI_POSTGRES_URL`: optional PostgreSQL persistence.
- `OSI_DAILY_LOSS_CAP`: blocks new signals when day PnL breaches the cap.
- `OSI_MAX_CONSECUTIVE_SL`: blocks new signals after repeated SL exits.
- `OSI_MAX_TRADE_NOTIONAL_RUPEES`: optional per-trade premium notional cap (`0` disables).
- `OSI_INTRABAR_REDIS_MIN_INTERVAL_MS`: throttle noisy intrabar Redis writes.
- `OSI_LATENCY_PATH_LOG_ENABLED`: structured latency path logging.
- `OSI_WS_RECONNECT_MAX_BACKOFF_SEC`: Angel websocket reconnect backoff.
- `OSI_WS_HEARTBEAT_STALE_SEC`: stale websocket heartbeat threshold.
- `OSI_OPTION_DATA_STALE_SEC`: option quote freshness limit where enabled.
- `OSI_TELEGRAM_ENABLED`, `OSI_TELEGRAM_BOT_TOKEN`, `OSI_TELEGRAM_CHAT_ID`: Telegram alerts/commands.

## API

- `GET /health`: service health.
- `GET /signals`: active dashboard cards.
- `GET /signals/active`: active signals.
- `GET /signals/history`: in-memory signal history.
- `GET /signals/rejected`: recent rejected signals and reasons.
- `GET /signals/paper`: paper/shadow engine signals.
- `GET /signals/paper/stats`: paper engine performance stats.
- `GET /signals/strategy`: latest per-engine strategy rows.
- `GET /history`: PostgreSQL history first, then memory fallback.
- `GET /metrics`: runtime, latency, trade, and risk metrics.
- `GET /market/indices`: live NIFTY/SENSEX index snapshot.
- `GET /dashboard`: static dashboard UI.
- `WS /signals/live`: live payload stream.

## Latency And Risk

Live payloads include:

```text
latency_ms.tick_to_process
latency_ms.process_to_signal
latency_ms.total_signal
entry_mode
market_phase
```

Logs include `LATENCY_PATH` when enabled:

```text
LATENCY_PATH path=<entry_mode> symbol=<symbol> tick_to_process_ms=... process_to_signal_ms=... total_signal_ms=... queue_depth=...
```

`GET /metrics` exposes rolling latency values, engine timing, rejected counts, and `risk` state.

Risk gates include manual pause, outside trade window, daily loss cap, consecutive SL cap, active duplicate protection, flip protection, cooldown, and optional notional cap.

## Data And Persistence

- `storage/signals.json` is runtime state: active signals, history, rejected signals, paper signals, and risk state.
- Redis key pattern: `osi:live:{symbol}` for latest live symbol payloads.
- PostgreSQL tables are created automatically when reachable:
  - `osi_signals`
  - `osi_engine_outputs`
- `analysis/` is for exported reports and research artifacts.
- `logs/` should stay local and ignored because broker SDK errors may contain sensitive context.

## Security Notes

- Keep credentials only in `.env` or environment variables.
- Do not commit `.env`, logs, `storage/signals.json`, large analysis exports, or browser binaries.
- `osi/core/log_config.py` attaches credential redaction filters for app and broker SDK loggers, but any historical logs created before redaction should be treated as compromised.
- Rotate broker credentials if they ever appeared in a log or chat.

## Telegram

When enabled, Telegram can send:

- system started snapshot
- signal created/updated/closed messages
- dashboard screenshots
- command responses such as pause/resume/status/active

Screenshots use Playwright, so installed browser binaries should remain outside git or ignored locally.

## ML Engine Training

Use sample training data when available:

```python
import json
from osi.engines.ml_engine import MLEngine

rows = json.load(open("osi/tests/ml_training_sample.json", "r", encoding="utf-8"))
ml = MLEngine()
ml.train(rows)
```

This writes `storage/models/osi_rf_model.joblib` for confidence boosting if that path is used.

## Operational Checklist

Before live trading:

- Confirm `.env` has current Angel credentials and no secrets are committed.
- Confirm system clock/timezone is correct.
- Confirm Redis/PostgreSQL status or accept fallbacks.
- Open `/metrics` and watch latency/risk after first ticks.
- Confirm `market_phase`, `engine_weights`, and `selected_engine` appear in live payloads/logs.
- Check Telegram pause/resume if Telegram is enabled.
- Keep the machine awake and avoid RAM/CPU-heavy workloads during market hours.

