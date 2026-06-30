@echo off
REM Run Release Radar without building an exe (needs Python 3.9+).
python -m pip install -r requirements.txt >nul 2>&1
python app.py
pause
