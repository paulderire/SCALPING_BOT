# CreateDesktopShortcut.ps1
# Run this ONCE to add "Scalping Bot" to your desktop and Windows startup.

$botDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$launcherBat = Join-Path $botDir "start_desktop_app.bat"
$icon = Join-Path $botDir "ScalpingBot.ico"  # optional custom icon

if (-not (Test-Path $launcherBat)) {
    throw "Launcher not found: $launcherBat"
}

$ws = New-Object -ComObject WScript.Shell

# Desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = $ws.CreateShortcut("$desktop\Scalping Bot.lnk")
$shortcut.TargetPath = $launcherBat
$shortcut.Arguments = ""
$shortcut.WorkingDirectory = $botDir
$shortcut.Description = "Gold Scalping Bot - Desktop App"
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()
Write-Host "Desktop shortcut created: $desktop\Scalping Bot.lnk"

# Windows Startup folder shortcut (auto-start on login)
$startup = [Environment]::GetFolderPath("Startup")
$shortcut2 = $ws.CreateShortcut("$startup\Scalping Bot.lnk")
$shortcut2.TargetPath = $launcherBat
$shortcut2.Arguments = ""
$shortcut2.WorkingDirectory = $botDir
$shortcut2.Description = "Gold Scalping Bot Desktop - auto-start"
if (Test-Path $icon) { $shortcut2.IconLocation = $icon }
$shortcut2.Save()
Write-Host "Startup shortcut created: $startup\Scalping Bot.lnk"

Write-Host ""
Write-Host "-----------------------------------------------"
Write-Host " Double-click 'Scalping Bot' on your desktop"
Write-Host " to launch the desktop dashboard app."
Write-Host "-----------------------------------------------"
