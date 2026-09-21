@echo off
rem ==============================================================================
rem Label Bench - Docker Compose Watchdog (Windows Batch)
rem ==============================================================================
rem Automatically keeps the Label Bench container running.
rem Usage:
rem   watchdog.bat            (runs compose up check every 10 seconds)
rem   watchdog.bat 15         (runs compose up check every 15 seconds)
rem ==============================================================================

setlocal enabledelayedexpansion

set "PORT=8013"
set "INTERVAL=10"
if not "%~1"=="" set "INTERVAL=%~1"

echo =================================================================
echo  Label Bench Watchdog (Windows)
echo =================================================================
echo  Target:  http://127.0.0.1:%PORT%/api/health
echo  Interval: %INTERVAL% seconds
echo =================================================================
echo Press Ctrl+C to stop.
echo.

:loop
curl -s -f -m 3 "http://127.0.0.1:%PORT%/api/health" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] Service unresponsive. Running docker compose up -d...
    docker compose up -d
) else (
    echo [%date% %time%] Label Bench is healthy.
)

timeout /t %INTERVAL% /nobreak >nul
goto loop
