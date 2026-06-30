@echo off
REM ============================================================
REM  Release Radar - build a standalone Windows .exe
REM  Double-click this file (or run it in a terminal).
REM  Requires Python 3.9+ installed and on your PATH.
REM ============================================================

echo.
echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err

echo.
echo Building ReleaseRadar.exe ...
REM --onefile   = single .exe
REM --noconsole = no terminal window (it runs in the background and opens your browser)
REM Remove --noconsole if you want to see logs in a console window for troubleshooting.
pyinstaller --onefile --noconsole --name ReleaseRadar app.py
if errorlevel 1 goto :err

echo.
echo ============================================================
echo  Done. Your program is here:   dist\ReleaseRadar.exe
echo  Double-click it to run. It opens the dashboard in your browser.
echo ============================================================
echo.
pause
goto :eof

:err
echo.
echo Build failed. Make sure Python is installed and on your PATH,
echo then run this file again.
echo.
pause
