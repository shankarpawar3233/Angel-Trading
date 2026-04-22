# OSI Latency & WebSocket Audit Pack

This document describes the **current implemented behavior** of OSI real-time ingestion, processing, and API delivery paths, based on live code.

---

## 1) Scope

Primary code paths reviewed:

- `osi/data/angel_ws.py`
- `osi/services/pipeline.py`
- `osi/services/signal_manager.py`
- `osi/services/candle_builder.py`
- `osi/services/metrics.py`
- `osi/api/routes.py`
- `osi/ui/dashboard.html`

---

## 2) WebSocket Flow (Angel)

## Connection and authentication sequence

1. `AngelWebSocketSource.start()` initializes:
   - event loop ref
   - instrument master
   - bridge task, heartbeat task, websocket stream task
2. `_run_smartapi_stream()` loop executes:
   - `_create_or_refresh_session()` (in `asyncio.to_thread`)
   - `_run_ws_blocking()` (in `asyncio.to_thread`)
3. `_create_or_refresh_session()`:
   - reads credentials from `OSI_ANGEL_*` settings or `ANGEL_*` env vars
   - creates `SmartConnect`
   - optional TOTP (`pyotp`) if secret exists
   - `generateSession(...)`
   - obtains auth token + feed token
4. `_run_ws_blocking()`:
   - creates `SmartWebSocketV2`
   - wires callbacks `on_open/on_data/on_error/on_close`
   - calls `.connect()`

## Subscription logic

- On `on_open`:
  - subscribes index tokens (`NIFTY`, `SENSEX`) via `_subscribe_index_tokens()`
  - re-applies option subscriptions via `_resubscribe_option_tokens()`
- Option subscription strategy:
  - ATM refresh when spot shifts by >= one strike step
  - selects three strike bands: `ATM-step`, `ATM`, `ATM+step`
  - both legs CE+PE
  - prefers today's expiry; fallback to nearest available if missing
- Token sync:
  - computes add/remove token sets
  - calls websocket `subscribe`/`unsubscribe` accordingly

## Reconnection and heartbeat

- Reconnect trigger sources:
  - websocket errors (`_on_error` closes connection)
  - stale heartbeat (`_heartbeat_loop` closes connection if no ticks for `ws_heartbeat_stale_sec`)
  - disconnect from server (`_on_close`)
- Retry strategy:
  - exponential backoff in `_run_smartapi_stream()`:
    - starts at 1s
    - doubles per failure
    - capped by `ws_reconnect_max_backoff_sec`
    - adds random jitter `0..0.5s`
- Re-subscription:
  - automatic on reconnect (`_on_open` -> `_resubscribe_option_tokens`)

## Tick timestamp source

- `MarketTick.timestamp` is assigned using `datetime.now(timezone.utc)` in `_emit()`
- `MarketTick.received_at` uses callback-side `now_epoch` converted to UTC
- Exchange-provided timestamp is **not** propagated into `MarketTick` currently.

---

## 3) Tick Processing Pipeline

Runtime path:

1. SmartAPI callback thread calls `AngelWebSocketSource._on_data(...)`
2. Rows parsed/normalized
3. Internal state updates:
   - index price map
   - option chain snapshot
4. `_emit(...)` builds `MarketTick`
5. Tick enqueued into thread-safe bridge queue (`queue.Queue`, max 20000)
6. Async bridge task `_bridge_loop` drains queue using `asyncio.to_thread(...)`
7. Bridge calls async handler (`pipeline.on_tick`)
8. Pipeline enqueues into async queue (`asyncio.Queue`, max 10000)
9. Pipeline worker consumes queue and runs:
   - intrabar path
   - candle-close engines
   - consensus + signal manager
   - redis/postgres snapshot and subscriber fan-out

## Blocking vs non-blocking behavior

- Callback thread:
  - non-blocking enqueue (`put_nowait`) into bridge queue
  - if full, tick is dropped (warning logged)
- Bridge to pipeline:
  - `pipeline.on_tick()` uses `await tick_queue.put(tick)` (blocking when full)
  - if async queue is saturated, bridge slows/stalls, which can eventually cause bridge queue growth and drop at source.

## Queue types

- Ingress bridge queue: `queue.Queue(maxsize=20000)` (thread-safe, callback thread -> async bridge)
- Processing queue: `asyncio.Queue(maxsize=10000)` (async worker pipeline queue)
- WS subscriber queue: `asyncio.Queue(maxsize=200)` per websocket client

---

## 4) Latency Measurement Logic

Defined in `pipeline.py` and recorded via `metrics.py`.

Measured metrics:

- `tick_to_process_latency_ms`
- `process_to_signal_latency_ms`
- `total_signal_latency_ms`

Calculation points (candle path):

- `tick_to_process_ms = now_utc - source_tick.received_at`
- `process_to_signal_ms = perf_counter_now - started`
- `total_signal_latency_ms = now_utc - source_tick.received_at`

Important note:

- These are based on **local receive/process times**, not exchange timestamp.
- `record_tick(elapsed_ms)` separately tracks worker-loop processing time per consumed queue item.

---

## 5) Queue Management and Backpressure

## Limits

- Bridge queue max: 20000
- Pipeline queue max: 10000

## Overflow behavior

- Bridge queue overflow: `_emit()` catches `queue.Full` and drops tick (`"Tick bridge queue full, dropping tick"`).
- Pipeline queue overflow: `await put()` blocks producer path (no immediate drop in async queue itself).

## Backpressure model

- Natural backpressure exists because bridge loop awaits `pipeline.on_tick()`.
- Under prolonged load:
  - pipeline queue fills
  - bridge loop blocks on put
  - bridge queue accumulates
  - source eventually drops when bridge queue max reached

No adaptive throttling or queue-priority mechanism is currently implemented.

---

## 6) Intrabar Data Handling

Implemented in `pipeline._intrabar_context(...)`:

- Stores tuples `(timestamp, index_price, instant_option_volume)` in list per symbol
- history cap: 120 entries (tick-count bounded)
- computes lookback window using **time-based** cutoff (`now_ts - 3.0`)
- extracts:
  - `ltp_now`
  - `ltp_3sec_ago` (first tick at/after lookback)
  - `volume_recent` (sum over lookback window)

Momentum in intrabar engine:

- `momentum = ltp_now - ltp_lookback`
- thresholded by configured `intrabar_momentum_threshold`

So storage is tick-list bounded but momentum lookup is time-window based.

---

## 7) Option Data Handling

## Option LTP update path

- In `_on_data`, option rows update in-memory chain:
  - key: symbol -> strike -> leg
  - fields: `ltp`, `volume`, `oi`, `expiry`

## Synchronization with index

- `_emit()` requires both:
  - index price present
  - option chain non-empty
- If either missing, emit is skipped and periodic info log generated.

## Freshness checks

- No explicit per-leg staleness timestamp validation.
- Chain is used as latest snapshot as-of callback updates.
- For signal generation, contract/ltp existence checks are done, but age/freshness SLA is not currently enforced.

---

## 8) Reconnect Logic Details

Reconnect triggers:

- callback error path (`_on_error`)
- heartbeat stale tick age > `ws_heartbeat_stale_sec`
- natural socket close

Retry strategy:

- exponential backoff with jitter
- capped max delay

Re-subscription:

- yes, automatic
- index + option subscriptions restored on each `on_open`

Time gap reconnect -> first tick:

- No dedicated metric tracked for this gap.
- Operationally depends on:
  - backoff delay at that attempt
  - auth/session latency
  - websocket open latency
  - feed resume latency

---

## 9) API Layer Latency Behavior

## `/signals`

- REST endpoint returns active cards from in-memory signal manager state.
- No explicit endpoint latency instrumentation in code.

## `/signals/live` WebSocket

- Server side:
  - each client gets an async queue via `pipeline.subscribe()`
  - worker pushes payloads on intrabar/candle/state updates
  - if subscriber queue full, payload for that client is skipped (`if not sub.full()`).

## Frontend transport behavior

Dashboard uses **both**:

- WebSocket live stream (`/signals/live`) for near-real-time updates
- Polling every 5s:
  - `/signals`
  - `/metrics`
  - `/history`
  - `/signals/active`
  - `/signals/history`

---

## 10) Timestamp Consistency

Current timestamp model:

- Ingestion timestamp:
  - `received_at`: local callback time (epoch converted to UTC)
  - `timestamp`: local UTC now in `_emit()`
- Candle timestamps:
  - derived from bucket boundaries (`_bucket_id`, `_bucket_start`) based on tick `timestamp`

Exchange timestamp usage:

- Not used in `MarketTick` currently.

Clock sync handling:

- No NTP/clock drift correction in application logic.

---

## 11) Edge Case Handling

## Tick delay > 1s

- No explicit delayed-tick rejection threshold.
- Heartbeat only checks long silence (`ws_heartbeat_stale_sec`), not per-tick lateness.

## Option data missing

- Emit skipped if chain empty or index missing.
- Signal manager rejects when option contract/ltp not found.

## Queue overflow

- Bridge queue overflow -> tick dropped with warning log.
- Async queue full -> producer waits (can propagate backpressure upstream).

## WebSocket disconnect mid-signal

- Active signal lifecycle continues using subsequent ticks after reconnect.
- During disconnect gap, no lifecycle updates occur.
- On reconnect, subscriptions are re-applied automatically.

---

## 12) Current Limitations / Known Gaps

1. **No exchange timestamp propagation** for latency truth; all timing is local-side.
2. **Reconnect-to-first-tick latency not explicitly measured**.
3. **No per-option freshness SLA checks** (age-based validation absent).
4. **Potential burst-loss path** under sustained overload due to bridge queue drops.
5. **No dedicated API handler latency metrics** (`/signals`, `/metrics`, etc.).
6. **Subscriber queue overflow silently skips per-client updates** (except implicit behavior).
7. **No adaptive load shedding policy** beyond fixed queue bounds.

---

## 13) File References

- `osi/data/angel_ws.py`
- `osi/services/pipeline.py`
- `osi/services/signal_manager.py`
- `osi/services/candle_builder.py`
- `osi/services/metrics.py`
- `osi/api/routes.py`
- `osi/ui/dashboard.html`

