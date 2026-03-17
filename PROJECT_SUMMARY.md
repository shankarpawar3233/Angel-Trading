# Angel Trading – Project Summary

## What We Are Doing

This repository hosts **two complementary, locally run systems** for the Indian equity/options market:

1. **free_quant_scalping_ai** – An AI-powered **options trading research platform** for NIFTY and SENSEX. It fetches live and historical data, runs ML/RL models, detects institutional flow and hero-zero opportunities, and serves scalping signals, gamma/expiry views, and a real-time dashboard. **No live trading**; research and signal generation only.

2. **trading_system** – A **NIFTY prediction system** that produces BUY / SELL / HOLD signals for the next 15 minutes (3 × 5‑minute candles). It uses Angel One SmartAPI for 5‑minute data (or mock data), trains an ML model, runs backtests, suggests options (CE/PE), and exposes a read-only dashboard. **Prediction only**; no order execution.

Both systems are designed to run **fully local** with optional Angel One API for live data.

---

## What We Have Done

### free_quant_scalping_ai

| Area | Done |
|------|------|
| **Backend** | FastAPI server with REST and WebSocket endpoints; CORS; configurable port (e.g. 8000/8001). |
| **Data** | SQLite schema (candles, option_chain, signals, option_ticks); historical fetch (Yahoo, 1m/5m/15m/1d); NSE option-chain fetch (with session/cookies); NSE live index prices (allIndices); **Angel One SmartAPI** login with dynamic TOTP; live index LTP (NIFTY/SENSEX); instrument master load and cache. |
| **Option discovery & streaming** | **Angel option discovery** from instrument master (NFO, NIFTY, OPTIDX/CE/PE); ATM ±20 strikes; **SmartWebSocketV2** subscription for NIFTY options; in-memory **live_option_chain** (ltp, volume, oi, oi_change); periodic persist to **option_ticks** table. |
| **Features** | Price features (returns, volatility, momentum, trend strength); technicals (RSI, MACD, EMA, ATR, Bollinger, VWAP); options features (PCR, OI aggregates); **OI analysis** (PCR, max OI call/put, OI spikes, volume spikes). |
| **Models** | Combined predictor (ensemble + deep scaffold); RandomForest + XGBoost ensemble; lazy load from `model.pkl`; daily retrain job (02:00 UTC). |
| **RL** | Gymnasium environment; Stable-Baselines3 PPO agent (optional); integrated into signal loop. |
| **Strategies** | Scalping engine (VWAP/EMA/RSI/volume/breakouts, ATM strike); hero-zero detector (premium &lt; 20, volume spike, OI); **hero_zero_live** (live option chain); **institutional flow engine** (long/short buildup, short covering, long unwinding); **liquidity sweep detector**; gamma exposure engine; expiry prediction (max pain, direction, range); **CE-PE signal engine** (merge ML, RL, scalping, hero-zero, flow, gamma, expiry, sweep, OI analysis). |
| **API** | `/market`, `/signals`, `/scalping`, `/hero-zero`, `/institutional-flow`, `/gamma`, `/expiry`, `/option-chain`, `/oi`, WebSocket `/ws/signals`. |
| **Dashboard** | React (Vite) + TailwindCSS + Chart.js; market strip (NIFTY/SENSEX, Angel/NSE/Cached); scalping cards; hero-zero list; institutional flow; **option chain heatmap**; **OI analysis** (PCR, max OI, spikes); gamma/expiry/liquidity sweep; ML/RL summary; polling every 3s. |
| **Automation** | APScheduler: daily model training; 60s option-tick persist when WebSocket is running; main loop (option chain fetch + compute per symbol every 60s). |
| **Run scripts** | `run.ps1`, `run.bat`; `main.py` entry point; optional `requirements-minimal.txt` (no PyTorch/RL). |

### trading_system

| Area | Done |
|------|------|
| **Data** | Angel One SmartAPI client for NIFTY 5‑min candles; fallback to mock OHLCV; raw → `storage/raw/nifty_5min.csv`. |
| **Features & labels** | Feature engineering; labeler (forward horizon, move threshold); processed → `storage/processed/features.csv`, `labeled_data.csv`. |
| **Model** | Trainer (e.g. RandomForest/XGBoost); save/load `trading_model.pkl`; metrics → `model_metrics.json`. |
| **Workflows** | Backtester; predictor (BUY/SELL/HOLD + confidence); options suggestion (CE/PE, ATM); one-shot `run_system.py` or `--live` (every 5 min). |
| **Visualization** | Charts (price + signals, feature importance, probability distribution, signal timeline, density heatmap); outputs under `storage/visuals/`. |
| **Dashboard** | FastAPI server (`run_dashboard.py`); HTML/JS/CSS UI; price chart, prediction card, options panel, signal history, model analytics; auto-refresh; `/docs`. |
| **Config** | `config/settings.py` (lookback, horizon, thresholds, model params, signal gates). |

---

## Technologies Used

### Backend & API

| Technology | Use |
|------------|-----|
| **Python 3.11** | Runtime for both projects. |
| **FastAPI** | REST API, request/response models, WebSocket. |
| **Uvicorn** | ASGI server. |
| **Pydantic / pydantic-settings** | Config and settings (`config/settings.py`). |
| **APScheduler** | Cron and interval jobs (training, persist, optional tasks). |

### Data & Storage

| Technology | Use |
|------------|-----|
| **SQLite** | Candles, option_chain, signals, option_ticks (free_quant_scalping_ai). |
| **Pandas** | DataFrames, feature tables, option chain processing. |
| **NumPy** | Numeric arrays and ML inputs. |
| **requests** | HTTP (NSE, Angel instrument URL, etc.). |
| **yfinance** | Historical index data (NIFTY/SENSEX). |

### Angel One Integration

| Technology | Use |
|------------|-----|
| **smartapi-python** | SmartConnect (login, LTP), SmartWebSocketV2 (option ticks). |
| **pyotp** | Dynamic TOTP for login. |
| **websocket-client** | WebSocket client for streaming. |
| **logzero** | Used by smartapi-python. |

### Machine Learning & Reinforcement Learning

| Technology | Use |
|------------|-----|
| **scikit-learn** | RandomForest, preprocessing, metrics. |
| **XGBoost** | Gradient boosting (ensemble, trading_system). |
| **PyTorch** | Deep model scaffold (LSTM/Transformer-style). |
| **Stable-Baselines3** | PPO RL agent. |
| **Gymnasium** | RL environment. |
| **joblib** | Model serialization (trading_system). |

### Frontend (free_quant_scalping_ai dashboard)

| Technology | Use |
|------------|-----|
| **React 18** | UI components. |
| **Vite** | Build and dev server. |
| **TailwindCSS** | Styling. |
| **Chart.js** | Charts (via react-chartjs-2). |

### Visualization & Reporting

| Technology | Use |
|------------|-----|
| **Plotly** | Optional server-side charts. |
| **Matplotlib** | Plots (trading_system visuals). |

### DevOps & Environment

| Technology | Use |
|------------|-----|
| **python-dotenv** | `.env` for secrets (Angel keys, TOTP, etc.). |
| **Git** | Version control; branch `anglefnotrading` tracked on GitHub. |

---

## Repository Layout

```
Angel Trading/
├── PROJECT_SUMMARY.md          # This file
├── .gitignore
├── free_quant_scalping_ai/     # Options research platform
│   ├── api/                    # FastAPI app, endpoints
│   ├── config/                 # Settings
│   ├── data/                   # Fetchers, Angel live/stream, storage
│   ├── features/               # Price, technicals, options, OI analysis
│   ├── models/                 # Predictor, ensemble, deep scaffold
│   ├── reinforcement/          # RL environment and agent
│   ├── strategies/             # Scalping, hero-zero, flow, gamma, expiry, CE-PE
│   ├── dashboard/react_dashboard/  # React + Vite + Tailwind
│   ├── main.py
│   └── requirements.txt
├── trading_system/             # NIFTY prediction system
│   ├── api/                    # Dashboard server
│   ├── config/                 # Settings, calendar
│   ├── data/                   # Angel client, data manager
│   ├── features/               # Feature engineering
│   ├── labels/                 # Labeler
│   ├── models/                 # Trainer
│   ├── workflows/              # Backtester, predictor, options suggestion
│   ├── visualization/          # Charts
│   ├── ui/                     # Dashboard HTML/JS/CSS
│   ├── storage/                # Raw, processed, models, visuals
│   └── run_system.py
├── run_system.py               # Root-level entry (if used)
└── package-lock.json
```

---

## How to Run

- **free_quant_scalping_ai:**  
  `cd free_quant_scalping_ai` → create venv, `pip install -r requirements.txt` (or `requirements-minimal.txt`) → `python main.py` or `.\run.ps1`.  
  Dashboard: `cd dashboard/react_dashboard` → `npm install` → `npm run dev`.

- **trading_system:**  
  `cd trading_system` → `pip install -r requirements.txt` → `python run_system.py` (or `python run_system.py --live`).  
  Dashboard: `python run_dashboard.py` → open http://127.0.0.1:8000.

---

## Notes

- **No live trading** is implemented; both projects are for **research, backtesting, and signal/options suggestion** only.
- Angel One credentials (and TOTP) are optional; without them, free_quant_scalping_ai falls back to NSE/Yahoo/cache, and trading_system can use mock data.
- NSE public APIs are unofficial; use for education and research only.
