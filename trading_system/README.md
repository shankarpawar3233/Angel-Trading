# NIFTY Prediction System

Lightweight ML-based prediction system for NIFTY index (5-minute candles). Produces **BUY / SELL / HOLD** signals for the next 15 minutes (3 candles). No trading execution—prediction only.

## Requirements

- Python 3.8+
- pandas, numpy, scikit-learn, xgboost, joblib, matplotlib

```bash
pip install -r requirements.txt
```

## Usage

**One-shot full pipeline** (fetch data → features → labels → train → predict → options → charts):

```bash
cd trading_system
python run_system.py
```

**Continuous prediction mode** (every 5 minutes):

```bash
cd trading_system
python run_system.py --live
```

## Data

- **Source:** Angel One SmartAPI (NSE, NIFTY, 5-min). If API is unavailable or not configured, mock OHLCV data is generated.
- **Lookback:** 90 trading days (configurable in `config/settings.py`).
- **Raw data:** `storage/raw/nifty_5min.csv`
- **Processed:** `storage/processed/features.csv`, `storage/processed/labeled_data.csv`, `storage/processed/backtest_results.csv`
- **Model:** `storage/models/trading_model.pkl`
- **Charts:** `storage/visuals/`

## Angel One API (optional)

Set environment variables to use live data:

- `ANGEL_API_KEY` or `SMARTAPI_KEY`
- `ANGEL_CLIENT_ID`
- `ANGEL_PASSWORD`
- `ANGEL_TOTP` (for 2FA)

Without these, the system uses generated mock data.

## Configuration

Edit `config/settings.py` for:

- `LOOKBACK_DAYS`, `FORWARD_HORIZON`, `MOVE_THRESHOLD`
- Model params: `N_ESTIMATORS`, `MAX_DEPTH`, `LEARNING_RATE`
- Signal gates: `MIN_PROBABILITY`, `MAX_VOLATILITY_RATIO`, `MIN_EMA_SLOPE`
- Market / prediction windows

## Dashboard (UI)

Web dashboard to view predictions and analytics (read-only, no trading):

```bash
cd trading_system
pip install fastapi uvicorn  # if not already installed
python run_dashboard.py
```

Open **http://127.0.0.1:8000**. The dashboard shows:

- NIFTY price chart with BUY/SELL markers
- Current prediction card and confidence gauge
- Options suggestion panel
- Signal history table
- Model analytics (accuracy, precision, confidence histogram, feature importance)

Auto-refresh every 5 minutes. API docs at http://127.0.0.1:8000/docs.

## Outputs

- **Predictions:** BUY / SELL / HOLD with confidence; only signals passing all gates (time, probability, volatility, trend) are marked as valid.
- **Options:** BUY → CE, SELL → PUT; ATM or slight ITM based on confidence.
- **Charts:** Price with signals, feature importance, probability distribution, signal timeline, signal density heatmap.
