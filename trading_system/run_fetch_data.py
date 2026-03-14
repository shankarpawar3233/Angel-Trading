#!/usr/bin/env python3
"""
Fetch and save NIFTY 5-minute historical data only.
Usage:
  python run_fetch_data.py
"""

import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(_SCRIPT_DIR)
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from data.data_manager import ensure_storage_dirs, update_raw_dataset
from config import settings


def main():
    print("=== NIFTY Historical Data Fetch ===\n")
    ensure_storage_dirs()
    df = update_raw_dataset()
    if df.empty:
        print("No data fetched.")
        return
    print(f"Saved {len(df)} candles to {settings.RAW_DATA_PATH}")
    print("\n=== Done ===")


if __name__ == "__main__":
    main()

