@echo off
:: Install Auto-Start for Scalping Bot
:: This adds the bot to Windows startup

echo ============================================
echo INSTALLING AUTO-START FOR SCALPING BOT
echo ============================================
echo.

:: Create startup shortcut
set STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set BOT_PATH=%~dp0
set SHORTCUT_PATH=%STARTUP_FOLDER%\ScalpingBot.lnk

:: Create VBS script to make shortcut
echo Set oWS = WScript.CreateObject("WScript.Shell") > "%TEMP%\CreateShortcut.vbs"
echo sLinkFile = "%SHORTCUT_PATH%" >> "%TEMP%\CreateShortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%TEMP%\CreateShortcut.vbs"
echo oLink.TargetPath = "%BOT_PATH%start_trading.bat" >> "%TEMP%\CreateShortcut.vbs"
echo oLink.WorkingDirectory = "%BOT_PATH%" >> "%TEMP%\CreateShortcut.vbs"
echo oLink.WindowStyle = 7 >> "%TEMP%\CreateShortcut.vbs"
echo oLink.Description = "Scalping Bot Auto-Start" >> "%TEMP%\CreateShortcut.vbs"
echo oLink.Save >> "%TEMP%\CreateShortcut.vbs"

cscript //nologo "%TEMP%\CreateShortcut.vbs"
del "%TEMP%\CreateShortcut.vbs"

echo.
echo [SUCCESS] Bot will auto-start when Windows boots
echo.
echo Shortcut created at:
echo %SHORTCUT_PATH%
echo.
echo To remove auto-start, run: uninstall_autostart.bat
echo.
pause
