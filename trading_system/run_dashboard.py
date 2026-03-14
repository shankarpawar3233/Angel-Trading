#!/usr/bin/env python3
"""
Start the NIFTY prediction dashboard (FastAPI + UI).
Run from trading_system directory: python run_dashboard.py
Then open http://127.0.0.1:8000
"""
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(_SCRIPT_DIR)
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import uvicorn
uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=False)
