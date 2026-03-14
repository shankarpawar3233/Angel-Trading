"""
Launcher: run from workspace root. Executes trading_system/run_system.py.
Usage: python run_system.py [--live]
"""
import subprocess
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
_sys_script = _root / "trading_system" / "run_system.py"
if not _sys_script.exists():
    print("trading_system/run_system.py not found.")
    sys.exit(1)
sys.exit(subprocess.call([sys.executable, str(_sys_script)] + sys.argv[1:]))
