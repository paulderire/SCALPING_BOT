@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_desktop_app.ps1"

if errorlevel 1 (
  echo [ERROR] Desktop launcher failed.
  pause
  exit /b 1
)

endlocal
