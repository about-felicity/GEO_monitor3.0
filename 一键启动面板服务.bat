@echo off
cd /d "%~dp0"
title Monitor Dashboard Services

echo Starting analytics, receivers and dashboard...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\operations\start_unified_monitor.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Check runtime\unified_control logs.
    pause
    exit /b 1
)

echo.
echo Dashboard services are ready: http://127.0.0.1:3000/
ping 127.0.0.1 -n 4 >nul
exit /b 0
