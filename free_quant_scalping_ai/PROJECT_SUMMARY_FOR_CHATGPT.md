# free_quant_scalping_ai — Project Summary for ChatGPT

Use this document as context when asking ChatGPT for help with this codebase. Copy and paste the relevant sections as needed.

---

## 1. What the project is

- **Name:** free_quant_scalping_ai  
- **Purpose:** Local, AI-assisted **options research platform** for Indian indices (NIFTY, BANK NIFTY, SENSEX). It does **not** place orders; it only produces signals and analytics for research/education.  
- **Repo path (example):** `d:\Angel Trading\free_quant_scalping_ai` (backend and dashboard live here).

---

## 2. Tech stack

| Layer | Stack |
|-------|--------|
| Backend | Python 3.11, FastAPI, uvicorn, APScheduler |
| Data | SQLite (storage), Angel One SmartAPI (live prices + option WebSocket), NSE (index price fallback), Yahoo Finance (historical candles) |
| ML/RL | PyTorch (LSTM), scikit-learn (RandomForest), XGBoost, Stable-Baselines3 (PPO), optional hmmlearn (regime HMM) |
| Dashboard | React, TailwindCSS, Chart.js, lightweight-charts |
| Config | pydantic-settings, .env, `config/settings.py` |

---

## 3. Project structure (key folders)

```
free_quant_scalping_ai/
├── main.py                 # Entry: runs uvicorn (api.api_server:app)
├── config/
│   └── settings.py        # PORT, indices, DB path, sequence_length, etc.
├── data/
│   ├── data_storage.py    # SQLite: candles, option_chain, option_ticks, signals, regime_history; init_schema, load_candles, store_signal, get_signal_history
│   ├── live_price.py      # get_live_price(symbol) → Angel → NSE → cached
│   ├── angel_option_stream.py   # WebSocket option chain; get_live_option_chain_snapshot()
│   ├── angel_option_discovery.py # Token discovery for NIFTY options (ATM band)
│   ├── historical_fetcher.py     # Yahoo Finance candles; bootstrap_historical_data()
│   └── nse_fetcher.py     # Deprecated for option chain (Angel only now)
├── features/
│   ├── option_chain_builder.py  # build_option_chain(snap), option_chain_to_dataframe()
│   ├── oi_analysis.py     # compute_oi_analysis() → PCR, max OI strikes, OI/volume spikes
│   ├── gamma_exposure.py  # compute_gamma_exposure() → gamma levels, walls, flip
│   ├── max_pain.py        # compute_max_pain() → max pain strike
│   ├── liquidity_map.py  # build_liquidity_map() → heatmap, support/resistance, stop clusters
│   ├── market_regime.py   # detect_market_regime() → TREND_UP/DOWN/RANGE/VOLATILE
│   ├── feature_engineering.py   # engineer_all_features(), add_vwap, RSI, ATR, EMAs
│   └── ...
├── models/
│   ├── predictor.py       # CombinedPredictor: LSTM + ensemble (RF+XGB) → BUY_CE/BUY_PE/NO_TRADE
│   ├── ensemble_model.py  # RandomForest + XGBoost
│   ├── deep_market_model.py # LSTMMarketModel (PyTorch)
│   └── regime_hmm.py      # predict_regime_hmm() (optional hmmlearn)
├── strategies/
│   ├── ce_pe_signal_engine.py # merge_signals(), build_final_signal(), _strategy_by_regime()
│   ├── scalping_engine.py     # generate_scalping_signal() → entry, target, stoploss, strike
│   ├── hero_zero_live.py      # detect_hero_zero_live() → cheap premium, volume spike, OI
│   ├── institutional_flow_engine.py # detect_institutional_flow_from_chain() → LONG_BUILDUP etc.
│   ├── stop_hunt_detector.py   # detect_stop_hunt() → liquidity grab detection
│   ├── liquidity_sweep_detector.py # detect_liquidity_sweep()
│   ├── gamma_exposure_engine.py    # analyze_gamma()
│   ├── expiry_prediction_engine.py # expiry_prediction()
│   └── expiry_direction.py     # compute_expiry_direction() → BULLISH/BEARISH
├── api/
│   └── api_server.py      # FastAPI app, startup, main_loop (60s), all REST + WebSocket
├── training/
│   └── daily_trainer.py   # train_models() — RF+XGB+LSTM; called daily 02:00 UTC
├── dashboard/
│   └── react_dashboard/   # React app; src/App.jsx, src/api.js
├── tests/
│   └── test_api_outputs.py # Hits all API endpoints (default port 8001)
├── storage/               # SQLite DB, Angel cache (created at runtime)
└── requirements.txt       # Full deps; requirements-minimal.txt for no PyTorch/RL
```

---

## 4. Data flow (backend)

1. **Startup:** `init_schema()`, `bootstrap_historical_data()` in executor, Angel option discovery + `start_option_stream()`, APScheduler (daily train 02:00 UTC, 60s persist option ticks), `main_loop()` every 60s.  
2. **Per symbol (e.g. NIFTY), each tick (~60s):**  
   - Load candles (1m → 5m → 15m → 1d fallback), engineer features, run ML predictor + optional RL.  
   - Option chain: Angel WebSocket snapshot → `option_chain_to_dataframe()` (fallback: DB `latest_option_chain`).  
   - Live price: `get_live_price(symbol)`.  
   - Compute: scalping, hero_zero_live, institutional_flow, liquidity_sweep, gamma (analyze_gamma), expiry_prediction, oi_analysis, gamma_exposure, max_pain, expiry_direction, liquidity_map, market_regime, optional regime_hmm, stop_hunt.  
   - `merge_signals(...)` → fused; `build_final_signal(symbol, price, fused)` → final_signal (includes entry, target, stoploss, strike from scalping or hero_zero).  
   - Store: state (market, signals, final_signal, market_regime, liquidity_map, stop_hunts), `store_regime()`, `store_signal(..., "combined", fused)`, `store_signal(..., "final", final_signal)`.  
3. **Option chain:** Built only from Angel WebSocket (no NSE option scraping). Structure: strike-keyed `{"23150": {"CE": {ltp, volume, oi, oi_change}, "PE": {...}}}`.

---

## 5. Final signal shape (what the system outputs)

Single object per symbol, stored and exposed via API and dashboard:

```json
{
  "symbol": "NIFTY",
  "price": 23150.5,
  "regime": "TREND_UP",
  "gamma_wall": 23200,
  "max_pain": 23150,
  "institutional_flow": "LONG_BUILDUP",
  "hero_zero": { "strike": 23150, "type": "CE", "entry": 15, "target": 45, "stoploss": 8, "probability": 70 },
  "trade": "BUY_CE",
  "confidence": 78,
  "entry": 15.0,
  "target": 21.0,
  "stoploss": 12.0,
  "strike": "23150 CE"
}
```

`entry`/`target`/`stoploss`/`strike` come from scalping signal or hero_zero candidate.

---

## 6. API endpoints (REST)

| Endpoint | Purpose |
|----------|---------|
| GET /market | Live index prices per symbol |
| GET /signals | Full signals blob per symbol |
| GET /final-signal | Final signal per symbol (above shape) |
| GET /signal-history?symbol=NIFTY&limit=50 | History of final signals (newest first) |
| GET /market-regime | Regime + confidence per symbol |
| GET /liquidity-map | Heatmap, support/resistance, stop clusters |
| GET /stop-hunts | Stop-hunt detection per symbol |
| GET /gamma-exposure | Gamma levels, walls, flip |
| GET /max-pain | Max pain strike |
| GET /hero-zero | Hero-zero candidates |
| GET /oi | OI analysis (PCR, max OI, spikes) |
| GET /expiry-bias | Expiry direction and range |
| GET /option-chain | Strike-keyed chain + analytics |
| GET /scalping | Scalping signal + liquidity sweep |
| GET /institutional-flow | Institutional flow summary |
| GET /gamma | Gamma (legacy) |
| GET /expiry | Expiry prediction |
| GET /candles?symbol=NIFTY&timeframe=5m&limit=200 | OHLC candles |
| WebSocket /ws/signals | Push: market, signals, final_signal, market_regime, liquidity_map, stop_hunts |

Default port 8000; override with env `PORT` (e.g. 8001).

---

## 7. Dashboard (React)

- **Location:** `dashboard/react_dashboard/`; API base from `VITE_API_URL` or `http://localhost:8001`.  
- **Panels:** Market strip (live prices), Final signal (trade, strike, entry, target, stoploss, confidence, regime, gamma_wall, max_pain, flow), **Signal history** (table: time, price, trade, entry, target, SL, confidence, regime), Market regime, Option chain heatmap, Liquidity map, Stop-hunt detection, NIFTY candles (1m/5m/15m/1d), Gamma exposure chart, Max pain, Expiry bias, Institutional flow, Hero-zero, OI analysis, Placed orders (paper), ML/RL.  
- **Polling:** Dashboard polls all endpoints every few seconds (e.g. 3s).

---

## 8. Database (SQLite)

- **Path:** `config.settings.DB_PATH` (e.g. `storage/market_data.sqlite`).  
- **Tables:**  
  - **candles** — symbol, timeframe, ts, open, high, low, close, volume  
  - **option_chain** — symbol, ts, strike, option_type, expiry, ltp, volume, oi, change_oi  
  - **option_ticks** — symbol, token, timestamp, ltp, volume, oi, oi_change  
  - **signals** — symbol, ts, category (e.g. "combined", "final"), payload_json  
  - **regime_history** — symbol, ts, regime, confidence, payload_json  

Signal history on the dashboard comes from `signals` with `category='final'` via `get_signal_history(symbol, category="final", limit)`.

---

## 9. Strategy switching by regime

- **TREND_UP** → emphasize breakout call (BUY_CE).  
- **TREND_DOWN** → emphasize breakout put (BUY_PE).  
- **RANGE** → mean reversion / scalping.  
- **VOLATILE** → hero-zero style trades.  

Implemented in `_strategy_by_regime()` and used inside `build_final_signal()` when choosing trade and fallback entry/target/stoploss from hero_zero.

---

## 10. How to run

```powershell
cd "d:\Angel Trading\free_quant_scalping_ai"
.\.venv\Scripts\python.exe main.py
# Or: $env:PORT=8001; .\.venv\Scripts\python.exe main.py
```

Dashboard (separate terminal):

```powershell
cd dashboard\react_dashboard
npm install
npm start
```

---

## 11. Conventions and gotchas

- **No broker order execution** — research only.  
- **Option chain source:** Angel WebSocket only; NSE option scraping is deprecated.  
- **Final signal** is the single source of truth for “what the system says” (trade, entry, target, stoploss, confidence, regime, etc.).  
- **Logging:** `[REGIME]`, `[HERO]`, `[GAMMA]`, `[FLOW]`, `[STOPHUNT]` used in api_server for key events.  
- **Python:** 3.11; paths and scripts assume Windows (e.g. `.venv\Scripts\python.exe`).

---

End of project summary. When asking ChatGPT for help, paste the sections that are relevant to your question (e.g. “Data flow”, “Final signal shape”, “API endpoints”, or “Project structure”).
