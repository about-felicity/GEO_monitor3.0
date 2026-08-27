@echo off
setlocal

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo Cannot find chrome.exe
  pause
  exit /b 1
)

start "" "%CHROME%" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%USERPROFILE%\ChromeSourceDebug" "about:blank"
