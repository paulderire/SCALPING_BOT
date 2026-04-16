@echo off
:: Stop all Scalping Bot instances
echo ============================================
echo STOPPING SCALPING BOT
echo ============================================
echo.

:: Kill by window title
echo Stopping ScalpingBot windows...
powershell -Command "Get-Process | Where-Object { $_.MainWindowTitle -like '*ScalpingBot*' } | Stop-Process -Force" 2>nul

:: Kill pythonw
echo Stopping pythonw.exe...
powershell -Command "Stop-Process -Name pythonw -Force -ErrorAction SilentlyContinue" 2>nul

:: Kill python processes running our scripts
echo Stopping python processes...
powershell -Command "Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | ForEach-Object { if ($_.CommandLine -like '*headless_bot*' -or $_.CommandLine -like '*dashboard*') { $_.Terminate() } }" 2>nul

echo.
echo [DONE] All bot instances stopped.
echo.
timeout /t 3
