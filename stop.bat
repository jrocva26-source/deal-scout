@echo off
REM ============================================================
REM Deal Scout - Stop Script
REM ============================================================
REM Kills both the watchdog and the bot process.

taskkill /F /IM pythonw.exe 2>nul
if %ERRORLEVEL% EQU 0 (
    echo Deal Scout stopped (watchdog + bot).
) else (
    REM Also try python.exe in case it was launched from terminal
    taskkill /F /FI "WINDOWTITLE eq *deal_scout*" 2>nul
    echo Deal Scout is not running.
)
