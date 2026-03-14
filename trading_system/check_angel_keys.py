#!/usr/bin/env python3
"""
Check if Angel One API keys are set and (optionally) if the API connection works.
Run from trading_system: python check_angel_keys.py
"""

import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

def _mask(s):
    if not s or len(s) < 4:
        return "***" if s else "(empty)"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]

def check_env():
    """Check which env vars are set (no secrets printed)."""
    vars_ = [
        ("ANGEL_API_KEY", os.environ.get("ANGEL_API_KEY") or os.environ.get("SMARTAPI_KEY")),
        ("ANGEL_CLIENT_ID", os.environ.get("ANGEL_CLIENT_ID")),
        ("ANGEL_PASSWORD", os.environ.get("ANGEL_PASSWORD")),
        ("ANGEL_TOTP", os.environ.get("ANGEL_TOTP")),
    ]
    all_set = True
    print("Environment variables:")
    for name, val in vars_:
        if val:
            print(f"  {name}: set ({_mask(val)})")
        else:
            print(f"  {name}: NOT SET")
            all_set = False
    return all_set

def check_api():
    """Try to get API session and fetch one candle."""
    from data.angel_client import _get_angel_api, _candle_from_angel
    from config import settings

    api = _get_angel_api()
    if api is None:
        print("\nAPI: Could not create session (check keys and pyotp: pip install pyotp).")
        return False
    print("\nAPI: Session created successfully.")
    # Try one small fetch
    from datetime import datetime, timedelta
    d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    candles = _candle_from_angel(
        api, settings.EXCHANGE, settings.SYMBOL_TOKEN,
        settings.INTERVAL, f"{d} 09:15", f"{d} 15:30"
    )
    if candles:
        print(f"API: Fetched {len(candles)} candle(s) for NIFTY. Keys are working.")
    else:
        print("API: Session OK but no candles returned (market holiday or time range).")
    return True

def main():
    print("=== Angel One keys check ===\n")
    if not check_env():
        print("\nSet missing variables (e.g. in PowerShell):")
        print("  $env:ANGEL_API_KEY   = 'your_key'")
        print("  $env:ANGEL_CLIENT_ID = 'your_client_id'")
        print("  $env:ANGEL_PASSWORD = 'your_password'")
        print("  $env:ANGEL_TOTP      = 'your_totp_secret'")
        sys.exit(1)
    print("\nAll required variables are set.")
    check_api()

if __name__ == "__main__":
    main()
