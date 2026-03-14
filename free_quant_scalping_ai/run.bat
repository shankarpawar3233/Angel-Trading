@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating venv with Python 3.11...
    py -3.11 -m venv .venv
)
echo Installing dependencies (first time may take 5-10 min)...
".venv\Scripts\python.exe" -m pip install -q -r requirements-minimal.txt
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements-minimal.txt
)
echo Starting server...
".venv\Scripts\python.exe" main.py
pause
