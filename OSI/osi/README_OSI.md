# Options Signal Intelligence (OSI)

Production-ready, event-driven options signal stack for Indian index options (NIFTY and SENSEX) with:

- FastAPI API
- Angel One SmartWebSocketV2 live tick ingestion
- Redis real-time state cache
- PostgreSQL historical persistence
- Modular strategy engines + weighted consensus
- Candle-close execution (1m and 3m)
- Signal lifecycle management (ACTIVE, TARGET_HIT, SL_HIT, CLOSED)

## Folder Structure

```text
osi/
  main.py
  api/
    routes.py
  core/
    config.py
    logging.py
    models.py
  data/
    angel_ws.py
  engines/
    scalping_engine.py
    smc_engine.py
    hero_zero_engine.py
    trend_engine.py
    mean_reversion_engine.py
    option_chain_engine.py
    ml_engine.py
    consensus_engine.py
  infra/
    redis_store.py
    postgres_repo.py
  services/
    candle_builder.py
    expiry_engine.py
    signal_manager.py
    metrics.py
    pipeline.py
```

## Run

1) Install dependencies:

```bash
pip install -r requirements.txt
```

2) Configure environment:

```bash
copy osi/config.sample.env .env
```

3) Fill live Angel values in `.env`:

- `OSI_ANGEL_API_KEY`
- `OSI_ANGEL_CLIENT_ID`
- `OSI_ANGEL_PASSWORD`
- `OSI_ANGEL_TOTP_SECRET`

4) Start API:

```bash
uvicorn osi.main:app --host 0.0.0.0 --port 8011 --reload
```

5) Open docs:

- <http://localhost:8011/docs>

## Endpoints

- `GET /signals` -> active dashboard cards
- `WS /signals/live` -> live event stream
- `GET /history` -> historical stored signals
- `GET /metrics` -> latency metrics (`tick_to_process`, `process_to_signal`, `total_signal`)
- `GET /health` -> service health

## ML Engine Training

Use sample training set:

```python
import json
from osi.engines.ml_engine import MLEngine

rows = json.load(open("osi/tests/ml_training_sample.json", "r", encoding="utf-8"))
ml = MLEngine()
ml.train(rows)
```

This creates `storage/models/osi_rf_model.joblib`, used for confidence boosting in live inference.

## Real-Time Data Pipeline

`SmartWebSocketV2 -> Tick Handler -> Candle Builder (1m/3m) -> Engines -> ML -> Consensus -> Signal Manager -> Redis/Postgres -> API/WebSocket clients`

Engines run only on candle close, never on every tick. Tick ingestion and publishing are fully event-driven (no polling).

## Dynamic Token Selection

- Subscribes index tokens first (`NIFTY`, `SENSEX`)
- Computes ATM from live index ticks:
  - NIFTY: nearest 50
  - SENSEX: nearest 100
- Selects strikes per symbol: `ATM - step`, `ATM`, `ATM + step`
- Loads only `OPTIDX` contracts from instrument master for nearest expiry
- Subscribes only selected CE/PE tokens (about 12 option tokens + 2 index tokens)

