@echo off
:: Run Headless Trading Bot (No Browser Required)
:: This runs 24/7 with auto-restart on crash

cd /d "%~dp0"
title Scalping Bot - Headless Mode

echo ============================================
echo HEADLESS TRADING BOT
echo ============================================
echo Strategy: SMC + ICT + Order Flow + Fibonacci
echo Symbols: GOLD, EURUSD, GBPUSD, BTCUSD
echo.
echo Logs saved to: logs\trading_YYYYMMDD.log
echo Press Ctrl+C to stop
echo ============================================
echo.

:: Create logs directory
if not exist logs mkdir logs

:: Activate virtual environment
call "%~dp0.venv\Scripts\activate.bat"

:loop
echo [%date% %time%] Starting bot...
python headless_bot.py

echo.
echo [%date% %time%] Bot stopped. Restarting in 30 seconds...
echo Press Ctrl+C to exit completely
timeout /t 30 /nobreak > nul
goto loop
