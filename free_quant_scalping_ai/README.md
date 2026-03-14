free_quant_scalping_ai
=======================

AI-powered, fully local options trading research platform for the Indian market (NIFTY / SENSEX) with:

- FastAPI backend and WebSockets
- ML / deep learning models (PyTorch, XGBoost, scikit-learn)
- Reinforcement learning agent (Stable-Baselines3 PPO)
- SQLite data storage
- React + TailwindCSS + Chart.js dashboard

All components run locally using only free, public data sources (NSE option-chain endpoints, Yahoo Finance historical data).

---

## Steps to run (quick start)

### 1. Prerequisites

- **Python 3.11** (install from [python.org](https://www.python.org/downloads/) or run `winget install Python.Python.3.11`).
- Open **PowerShell** or **Command Prompt**.

### 2. Go to the project folder

```powershell
cd "d:\Angel Trading\free_quant_scalping_ai"
```

*(Use your actual path if different.)*

### 3. Create virtual environment (first time only)

```powershell
py -3.11 -m venv .venv
```

Skip this if `.venv` already exists.

### 4. Install dependencies (first time only)

**Option A – Minimal (faster, no PyTorch/RL):**

```powershell
.\.venv\Scripts\pip.exe install -r requirements-minimal.txt
```

**Option B – Full (ML + RL; takes longer, large downloads):**

```powershell
.\.venv\Scripts\pip.exe install -r requirements.txt
```

### 5. Start the backend

**Option A – Using the run script (easiest)**

- **PowerShell:** `.\run.ps1`
- **Or double‑click:** `run.bat`

**Option B – Using the venv Python directly**

```powershell
.\.venv\Scripts\python.exe main.py
```

**If port 8000 is already in use**, use another port:

```powershell
$env:PORT=8001; .\.venv\Scripts\python.exe main.py
```

### 6. Open the app

- **API docs (Swagger):** http://localhost:8000/docs  
- **If you used port 8001:** http://localhost:8001/docs  
- **Market data:** http://localhost:8000/market (or 8001)

The server will download historical data in the background and then run the signal loop every minute.

---

## 1. Project structure

- **config**: configuration and settings
- **data**: data fetchers, streaming, and SQLite storage
- **features**: feature engineering for price, technicals, options, and market structure
- **models**: ML/deep models and unified predictor
- **reinforcement**: RL environment and trading agent
- **strategies**: scalping / hero-zero / institutional flow / gamma / expiry / CE-PE engines
- **backtesting**: backtest engine and metrics
- **dashboard/react_dashboard**: React + Tailwind + Chart.js UI
- **api**: FastAPI app and REST/WebSocket endpoints
- **utils**: logging and helpers
- **main.py**: orchestrates data fetch, feature update, models, RL, and signal generation

## 2. Installation (backend)

```bash
cd free_quant_scalping_ai
python -m venv .venv
.venv\Scripts\activate  # On Windows

pip install -r requirements.txt
```

## 3. Running the backend

### Option A – No activation (recommended on Windows)

From the project folder, use the venv’s Python so you don’t need to activate or change execution policy:

```powershell
cd "d:\Angel Trading\free_quant_scalping_ai"
.\.venv\Scripts\python.exe main.py
```

Or double‑click **`run.bat`**, or in PowerShell run **`.\run.ps1`**.

### Option B – Activate then run

If PowerShell says “running scripts is disabled”, either use Option A above or allow scripts for your user (once):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

### If you see `ModuleNotFoundError: No module named 'yfinance'`

You’re not using the venv (e.g. you ran `python` without activating). Install deps with the venv’s pip and run with the venv’s python:

```powershell
.\.venv\Scripts\pip.exe install -r requirements-minimal.txt
.\.venv\Scripts\python.exe main.py
```

---

This will:

- Start the FastAPI server (REST + WebSockets)
- Initialize SQLite database
- Start periodic background tasks for:
  - Fetching NIFTY and SENSEX prices
  - Fetching option-chain data (NSE public endpoints)
  - Updating features and signals
  - Running ML models and RL agent for research signals

API base URL (default):

- `http://localhost:8000`

Key endpoints:

- `/market`: latest market snapshot
- `/signals`: combined signal view
- `/scalping`: scalping signals
- `/hero-zero`: hero-zero candidates
- `/institutional-flow`: institutional flow signals
- `/gamma`: gamma exposure view
- `/expiry`: expiry prediction view

## 4. Running the dashboard

```bash
cd dashboard/react_dashboard
npm install
npm start
```

This runs the React dashboard (default `http://localhost:3000`) which connects to the FastAPI backend for:

- Live NIFTY / SENSEX prices
- Scalping and hero-zero signals
- Option-chain heatmaps
- Institutional flow / gamma exposure / expiry prediction panels

## 5. Data sources (FREE only)

- **NSE option chain (indices)**: public JSON API (unofficial, may require desktop browser headers)
  - `https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY`
  - `https://www.nseindia.com/api/option-chain-indices?symbol=BANKNIFTY`
- **Yahoo Finance historical** (via `yfinance`):
  - NIFTY: `^NSEI`
  - SENSEX: `^BSESN`

The platform downloads at least 1 year of historical data for:

- Timeframes: 1m, 5m, 15m, 1D

and stores them locally in SQLite for model training and backtesting.

## 6. Notes and limitations

- NSE public APIs are unofficial and may change or throttle access; this project is for **research and education only**, not for production trading.
- The initial models and RL agent are configured with simple architectures and example hyperparameters; you should tune them with your own research and risk management.
- No broker integration is implemented; signals are research-only.

