@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
python -u -m web_collectors.remote_worker
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
