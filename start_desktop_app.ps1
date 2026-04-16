$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$dashboard = Join-Path $PSScriptRoot 'dashboard.py'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$profileDir = Join-Path $PSScriptRoot '.desktop_edge_profile'
$statusUrl = 'http://127.0.0.1:5000/api/status'
$connectUrl = 'http://127.0.0.1:5000/api/connect'

if (-not (Test-Path $python)) {
    throw 'Virtual environment not found at .venv\Scripts\python.exe'
}

if (-not (Test-Path $edge)) {
    throw 'Microsoft Edge was not found at the expected location.'
}

function Test-ServerUp {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing $statusUrl -TimeoutSec 3
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-ServerUp)) {
    $backendArgs = @('-B', $dashboard)
    $backend = Start-Process -FilePath $python -ArgumentList $backendArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-ServerUp) { break }
        Start-Sleep -Seconds 1
    }

    if (-not (Test-ServerUp)) {
        try { Stop-Process -Id $backend.Id -Force } catch {}
        throw 'Dashboard server did not start in time.'
    }
}

try {
    Invoke-WebRequest -UseBasicParsing $connectUrl -Method POST -TimeoutSec 10 | Out-Null
} catch {
    # Non-fatal.
}

if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir | Out-Null
}

$appArgs = @(
    '--app=http://127.0.0.1:5000',
    '--new-window',
    '--window-size=1440,900',
    '--window-position=40,40',
    "--user-data-dir=$profileDir",
    '--no-first-run',
    '--disable-features=TranslateUI'
)

$edgeProc = Start-Process -FilePath $edge -ArgumentList $appArgs -PassThru
try {
    Wait-Process -Id $edgeProc.Id
} finally {
    try { $edgeProc.CloseMainWindow() | Out-Null } catch {}
}
