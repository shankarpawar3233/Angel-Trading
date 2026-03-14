"""
REST API for NIFTY prediction dashboard. Read-only; no trading.
"""

import json
import re
import sys
from pathlib import Path

# Run from trading_system directory
_SCRIPT_DIR = Path(__file__).resolve().parents[1]
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="NIFTY Prediction API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths relative to trading_system/
STORAGE = _SCRIPT_DIR / "storage"
RAW_PATH = STORAGE / "raw" / "nifty_5min.csv"
BACKTEST_PATH = STORAGE / "processed" / "backtest_results.csv"
LIVE_PATH = STORAGE / "processed" / "live_predictions.csv"
METRICS_PATH = STORAGE / "processed" / "model_metrics.json"
MODEL_PATH = STORAGE / "models" / "trading_model.pkl"


def _parse_suggested_option(opt_str: str) -> tuple:
    """Parse 'NIFTY 22450 CE' -> ('CALL', 22450). CE->CALL, PE->PUT."""
    if not opt_str or not isinstance(opt_str, str):
        return None, None
    m = re.search(r"NIFTY\s+(\d+)\s+(CE|PE)", opt_str, re.I)
    if not m:
        return None, None
    strike = int(m.group(1))
    kind = "CALL" if m.group(2).upper() == "CE" else "PUT"
    return kind, strike


def _load_backtest_df() -> pd.DataFrame:
    """Load backtest predictions from CSV."""
    if not BACKTEST_PATH.exists():
        return pd.DataFrame()
    df = pd.read_csv(BACKTEST_PATH)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _load_predictions_df() -> pd.DataFrame:
    """
    Load predictions for dashboard:
    - Prefer live_predictions.csv if it exists and has rows
    - Otherwise fall back to backtest_results.csv
    """
    if LIVE_PATH.exists():
        try:
            df_live = pd.read_csv(LIVE_PATH)
            if not df_live.empty:
                if "timestamp" in df_live.columns:
                    df_live["timestamp"] = pd.to_datetime(df_live["timestamp"])
                return df_live
        except Exception:
            pass
    return _load_backtest_df()


@app.get("/api/latest_prediction")
def get_latest_prediction():
    """Latest single prediction for dashboard."""
    df = _load_predictions_df()
    if df.empty or len(df) == 0:
        return {
            "timestamp": None,
            "spot_price": None,
            "prediction": "HOLD",
            "confidence": 0.0,
            "suggested_option": None,
            "suggested_strike": None,
        }
    row = df.iloc[-1]
    ts = row["timestamp"]
    if hasattr(ts, "strftime"):
        ts = ts.strftime("%Y-%m-%d %H:%M")
    opt_str = row.get("suggested_option") or ""
    opt_type, strike = _parse_suggested_option(opt_str)
    # ATM / ITM / OTM variants if present
    atm = row.get("suggested_option_atm") if "suggested_option_atm" in df.columns else ""
    itm = row.get("suggested_option_itm") if "suggested_option_itm" in df.columns else ""
    otm = row.get("suggested_option_otm") if "suggested_option_otm" in df.columns else ""
    return {
        "timestamp": ts,
        "spot_price": float(row.get("spot_price", 0)),
        "prediction": str(row.get("signal", "HOLD")),
        "confidence": round(float(row.get("confidence", 0)), 4),
        "suggested_option": opt_type,
        "suggested_strike": strike,
        "suggested_option_atm": atm or None,
        "suggested_option_itm": itm or None,
        "suggested_option_otm": otm or None,
    }


@app.get("/api/prediction_history")
def get_prediction_history(limit: int = 100):
    """Last N predictions."""
    import math
    try:
        df = _load_predictions_df()
        if df.empty:
            return {"predictions": []}
        df = df.tail(int(limit)).copy()
        df["timestamp"] = df["timestamp"].apply(
            lambda x: x.strftime("%Y-%m-%d %H:%M") if hasattr(x, "strftime") else str(x)
        )
        records = []
        has_atm = "suggested_option_atm" in df.columns
        for _, row in df.iterrows():
            opt_str = row.get("suggested_option") or ""
            opt_type, strike = _parse_suggested_option(opt_str)
            atm = row.get("suggested_option_atm") if has_atm else ""
            itm = row.get("suggested_option_itm") if has_atm else ""
            otm = row.get("suggested_option_otm") if has_atm else ""
            # Safe confidence handling (avoid NaN in JSON)
            conf_raw = row.get("confidence", 0)
            try:
                f = float(conf_raw)
                conf_val = None if (f != f or math.isnan(f)) else round(f, 4)
            except Exception:
                conf_val = None
            # Safe spot price
            spot_raw = row.get("spot_price", 0)
            try:
                spot_val = float(spot_raw)
            except Exception:
                spot_val = 0.0
            records.append({
                "timestamp": row["timestamp"],
                "spot_price": spot_val,
                "prediction": str(row.get("signal", "HOLD")),
                "confidence": conf_val,
                # Use ATM as main suggested option in table if available
                "suggested_option": atm or opt_str or (f"NIFTY {strike} {opt_type}" if strike else ""),
                "suggested_option_atm": atm or None,
                "suggested_option_itm": itm or None,
                "suggested_option_otm": otm or None,
            })
        return {"predictions": list(reversed(records))}
    except Exception:
        # Never 500 for history; return empty list on error
        return {"predictions": []}


@app.get("/api/model_stats")
def get_model_stats():
    """Accuracy, precision, confidence distribution from last run."""
    import math
    out = {
        "accuracy": None,
        "precision": None,
        "confidence_values": [],
    }
    try:
        if METRICS_PATH.exists():
            with open(METRICS_PATH) as f:
                data = json.load(f)
            out["accuracy"] = data.get("accuracy")
            out["precision"] = data.get("precision_macro")
        df = _load_predictions_df()
        if not df.empty and "confidence" in df.columns:
            out["confidence_values"] = [
                None if (x != x or math.isnan(x)) else round(float(x), 4)
                for x in df["confidence"]
            ]
    except Exception:
        pass
    return out


@app.get("/api/chart_data")
def get_chart_data(limit: int = 500):
    """OHLC + signals for candlestick and markers."""
    ohlc = []
    if RAW_PATH.exists():
        df_raw = pd.read_csv(RAW_PATH)
        df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"])
        df_raw = df_raw.tail(int(limit))
        for _, r in df_raw.iterrows():
            ts = r["timestamp"]
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            ohlc.append({
                "timestamp": ts_str,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r.get("volume", 0)),
            })
    df_bt = _load_predictions_df()
    signals = []
    if not df_bt.empty:
        df_bt = df_bt.tail(int(limit))
        for _, r in df_bt.iterrows():
            ts = r["timestamp"]
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            sig = str(r.get("signal", "HOLD"))
            if sig in ("BUY", "SELL"):
                signals.append({
                    "timestamp": ts_str,
                    "signal": sig,
                    "price": float(r.get("spot_price", 0)),
                    "confidence": round(float(r.get("confidence", 0)), 4),
                })
    return {"ohlc": ohlc, "signals": signals}


@app.get("/api/feature_importance")
def get_feature_importance():
    """Feature importance from trained model for chart."""
    try:
        import joblib
        if not MODEL_PATH.exists():
            return {"features": [], "importance": []}
        art = joblib.load(MODEL_PATH)
        model = art.get("model")
        cols = art.get("feature_columns", [])
        if not cols:
            return {"features": [], "importance": []}
        if hasattr(model, "calibrated_classifiers_") and len(model.calibrated_classifiers_) > 0:
            base = getattr(model.calibrated_classifiers_[0], "estimator", model.calibrated_classifiers_[0])
        else:
            base = model
        if not hasattr(base, "feature_importances_"):
            return {"features": cols, "importance": [0.0] * len(cols)}
        imp = base.feature_importances_
        return {"features": cols, "importance": [float(x) for x in imp]}
    except Exception:
        return {"features": [], "importance": []}


# Serve UI from parent's ui/ folder
UI_DIR = _SCRIPT_DIR / "ui"
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

    @app.get("/")
    def serve_dashboard():
        return FileResponse(UI_DIR / "index.html")
else:
    @app.get("/")
    def root():
        return {"message": "NIFTY Prediction API", "docs": "/docs", "ui": "Place index.html in ui/"}
