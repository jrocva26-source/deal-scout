@echo off
REM ============================================================
REM Deal Scout - Start Script
REM ============================================================
REM Launches the watchdog (which auto-restarts the bot on crash).
REM Runs silently with no console window.
REM
REM Logs:
REM   deal_scout.log  - bot activity, deals, errors
REM   watchdog.log    - start/stop/crash/restart events
REM
REM To stop:  run stop.bat
REM ============================================================

cd /d "%~dp0"

REM Force Python UTF-8 mode
set PYTHONUTF8=1

REM Check if already running
tasklist /FI "IMAGENAME eq pythonw.exe" /NH 2>nul | findstr /I "pythonw" >nul
if %ERRORLEVEL% EQU 0 (
    echo Deal Scout is already running.
    echo Run "stop.bat" first if you want to restart.
    pause
    exit /b 1
)

REM Launch watchdog detached (pythonw = no console window)
start "" /B "%~dp0venv\Scripts\pythonw.exe" "%~dp0watchdog.py"

echo Deal Scout started! (with auto-restart watchdog)
echo.
echo   Logs:  deal_scout.log, watchdog.log
echo   Stop:  run stop.bat
