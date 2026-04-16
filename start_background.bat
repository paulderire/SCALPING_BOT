@echo off
:: Scalping Bot - Background Service
:: Runs headless bot (no browser needed)
:: To stop: run stop_bot.bat
::
:: WARNING: This launches a FULLY AUTONOMOUS trading bot (headless_bot.py).
:: It will open live trades immediately without any dashboard confirmation.
:: Only run this if you intentionally want unattended 24/7 trading.

cd /d "%~dp0"

echo ============================================
echo WARNING: HEADLESS BOT - LIVE TRADING
echo ============================================
echo This will start an autonomous trading bot
echo that opens REAL trades without confirmation.
echo.
echo Press CTRL+C to cancel, or any key to continue...
pause > nul

:: Create logs directory
if not exist logs mkdir logs

:: Activate venv and start headless bot minimized
start /min "ScalpingBot" cmd /c "call .venv\Scripts\activate.bat && python headless_bot.py"

echo.
echo ============================================
echo BOT STARTED IN BACKGROUND
echo ============================================
echo.
echo Mode: Headless (no browser required)
echo Logs: logs\trading_*.log
echo.
echo To use Desktop App instead: double-click ScalpingBot.vbs
echo To stop: run stop_bot.bat
echo.
timeout /t 5
