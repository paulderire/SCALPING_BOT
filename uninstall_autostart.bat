@echo off
:: Remove Auto-Start for Scalping Bot

echo ============================================
echo REMOVING AUTO-START FOR SCALPING BOT
echo ============================================
echo.

set SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ScalpingBot.lnk

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo [SUCCESS] Auto-start removed
) else (
    echo [INFO] Auto-start was not installed
)

echo.
pause
