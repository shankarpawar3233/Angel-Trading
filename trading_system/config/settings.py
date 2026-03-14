"""
Centralized configuration for NIFTY prediction system.
"""

# Data
LOOKBACK_DAYS = 90
INTERVAL = "FIVE_MINUTE"
EXCHANGE = "NSE"
SYMBOL = "NIFTY"
SYMBOL_TOKEN = 26000

# Market hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
NO_TRADE_START_HOUR = 12
NO_TRADE_START_MINUTE = 0
NO_TRADE_END_HOUR = 13
NO_TRADE_END_MINUTE = 30

# Allowed prediction window
PRED_START_HOUR = 9
PRED_START_MINUTE = 20
PRED_END_HOUR = 15
PRED_END_MINUTE = 15

# Features
EMA_PERIOD = 21
ATR_PERIOD = 14
RSI_PERIOD = 14

# Labels
FORWARD_HORIZON = 3  # candles ahead
MOVE_THRESHOLD = 0.002  # 0.2%

# Model
MODEL_TYPE = "xgboost"
TRAIN_SPLIT = 0.75
N_ESTIMATORS = 100
MAX_DEPTH = 5
LEARNING_RATE = 0.1

# Signal gates
MIN_PROBABILITY = 0.62
MAX_VOLATILITY_RATIO = 1.5
MIN_EMA_SLOPE = 0.0001

# Paths (relative to project root)
STORAGE_ROOT = "storage"
RAW_DATA_PATH = "storage/raw/nifty_5min.csv"
PROCESSED_FEATURES_PATH = "storage/processed/features.csv"
LABELED_DATA_PATH = "storage/processed/labeled_data.csv"
MODEL_PATH = "storage/models/trading_model.pkl"
BACKTEST_RESULTS_PATH = "storage/processed/backtest_results.csv"
VISUALS_DIR = "storage/visuals"

# Capital (for display / future use)
CAPITAL = 100000

# Live mode
LIVE_INTERVAL_MINUTES = 5
