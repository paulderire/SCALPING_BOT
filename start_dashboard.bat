@echo off
title Gold Scalper Bot - Dashboard
echo ============================================================
echo      MULTI-ASSET SCALPER BOT - DASHBOARD (BTC ENABLED)
echo ============================================================
echo.
echo Starting web dashboard...
echo Dashboard will be available at: http://localhost:5000
echo Symbols: GOLD, EURUSD, GBPUSD, BTCUSD
echo.
echo Keep this window open to keep the bot running.
echo Close this window to stop the bot.
echo.
cd /d "%~dp0"
for /f %%p in ('powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue ^| Select-Object -ExpandProperty OwningProcess -Unique"') do (
	if not "%%p"=="" taskkill /PID %%p /F >nul 2>&1
)
set PYTHONDONTWRITEBYTECODE=1
"%~dp0.venv\Scripts\python.exe" -B "%~dp0dashboard.py"
pause
