@echo off
:: Scalping Bot Auto-Start Script
:: Waits for MT5 and starts trading automatically

cd /d "%~dp0"

echo ============================================
echo SCALPING BOT - AUTO START
echo ============================================
echo Starting at: %date% %time%
echo.

:: Wait for network (30 seconds)
echo Waiting for network connection...
timeout /t 30 /nobreak > nul

:: Try to start MT5 if not running
echo Checking MetaTrader 5...
tasklist /fi "imagename eq terminal64.exe" 2>nul | find /i "terminal64.exe" > nul
if errorlevel 1 (
    echo Starting MetaTrader 5...
    
    :: Try common MT5 installation paths
    if exist "C:\Program Files\MetaTrader 5\terminal64.exe" (
        start "" "C:\Program Files\MetaTrader 5\terminal64.exe"
    ) else if exist "C:\Program Files\XM MT5\terminal64.exe" (
        start "" "C:\Program Files\XM MT5\terminal64.exe"
    ) else if exist "%APPDATA%\MetaTrader 5\terminal64.exe" (
        start "" "%APPDATA%\MetaTrader 5\terminal64.exe"
    ) else (
        echo [WARNING] MT5 not found in common paths
        echo Please start MT5 manually or update the path in this script
    )
    
    :: Wait for MT5 to fully load (90 seconds)
    echo Waiting for MT5 to initialize...
    timeout /t 90 /nobreak > nul
) else (
    echo MT5 is already running
)

:: Activate virtual environment and start dashboard
echo Starting Trading Dashboard...
call "%~dp0.venv\Scripts\activate.bat"

:: Start the dashboard (will run in this window)
python dashboard.py

:: If dashboard exits, wait before closing
echo.
echo [WARNING] Dashboard stopped
pause
