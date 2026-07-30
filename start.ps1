<#
.SYNOPSIS
    Start or restart the Evidence Monitoring Agent (backend + frontend) on Windows.

.DESCRIPTION
    Frees the dev ports (stopping anything already listening = "restart"), then
    launches the FastAPI backend and the Vite frontend, each in its own window.

      - Backend : .venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8001  (cwd = backend)
      - Frontend: npm run dev                                                            (cwd = frontend, Vite :5173)

    The Vite dev server proxies /api -> http://127.0.0.1:8001, so the backend
    must run on 8001 for the dashboard to talk to the API.

.PARAMETER BackendPort
    Port for the FastAPI/uvicorn backend. Default 8001 (keep this to match the Vite proxy).

.PARAMETER FrontendPort
    Port for the Vite dev server. Default 5173.

.PARAMETER BackendOnly
    Start/restart only the backend.

.PARAMETER FrontendOnly
    Start/restart only the frontend.

.PARAMETER Stop
    Stop the servers (free the ports) and exit without starting anything.

.PARAMETER Install
    Force dependency install before starting (npm install for the frontend).
    (node_modules is auto-installed when missing regardless of this flag.)

.PARAMETER Open
    Open the dashboard in the default browser once started.

.EXAMPLE
    .\start.ps1                # start or restart both
    .\start.ps1 -BackendOnly   # just the API
    .\start.ps1 -Stop          # stop both
    .\start.ps1 -Open          # start both and open the browser
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8001,
    [int]$FrontendPort = 5173,
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$Stop,
    [switch]$Install,
    [switch]$Open
)

$ErrorActionPreference = 'Stop'

# --- Paths (this script lives at the repo root) -----------------------------
$Root        = $PSScriptRoot
$BackendDir  = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$VenvPython  = Join-Path $Root '.venv\Scripts\python.exe'

$doBackend  = -not $FrontendOnly
$doFrontend = -not $BackendOnly

# --- Helpers ----------------------------------------------------------------
function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

# Preferred shell for the spawned windows (user runs pwsh; fall back to Windows PowerShell).
$Shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }

function Stop-Port {
    param([int]$Port, [string]$Label)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { Write-Ok "$Label port $Port is free."; return }
    $pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -and $_ -ne 0 })
    foreach ($procId in $pids) {
        try {
            $p = Get-Process -Id $procId -ErrorAction Stop
            Write-Warn2 "stopping existing $Label (PID $procId, $($p.ProcessName)) on port $Port"
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-Warn2 "could not stop PID $procId on port $Port : $($_.Exception.Message)"
        }
    }
    # Give the OS a moment to release the socket.
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 150
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            Write-Ok "$Label port $Port freed."
            return
        }
    }
    Write-Warn2 "$Label port $Port still appears busy; the new process may fail to bind."
}

function Start-InNewWindow {
    param([string]$Title, [string]$WorkDir, [string]$Command)
    # -NoExit keeps the window open so logs stay visible.
    $inner = "`$host.UI.RawUI.WindowTitle = '$Title'; $Command"
    Start-Process -FilePath $Shell -WorkingDirectory $WorkDir `
        -ArgumentList '-NoLogo', '-NoExit', '-Command', $inner | Out-Null
    Write-Ok "launched '$Title' in a new $Shell window."
}

# --- Stop phase (also the first half of a restart) --------------------------
Write-Step "Stopping any running dev servers"
if ($doBackend)  { Stop-Port -Port $BackendPort  -Label 'backend'  }
if ($doFrontend) { Stop-Port -Port $FrontendPort -Label 'frontend' }

if ($Stop) {
    Write-Step "Done. Servers stopped."
    return
}

# --- Preflight checks -------------------------------------------------------
if ($doBackend -and -not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at '$VenvPython'. Create it first:`n  python -m venv .venv`n  .venv\Scripts\Activate.ps1`n  pip install -r backend\requirements.txt"
    return
}
if ($doFrontend) {
    $nodeModules = Join-Path $FrontendDir 'node_modules'
    if ($Install -or -not (Test-Path $nodeModules)) {
        Write-Step "Installing frontend dependencies (npm install)"
        Push-Location $FrontendDir
        try { & npm install } finally { Pop-Location }
    }
}

# --- Start phase ------------------------------------------------------------
if ($doBackend) {
    Write-Step "Starting backend (uvicorn) on http://127.0.0.1:$BackendPort"
    $backendCmd = "& '$VenvPython' -m uvicorn app.main:app --reload --port $BackendPort"
    Start-InNewWindow -Title "EMA Backend :$BackendPort" -WorkDir $BackendDir -Command $backendCmd
}

if ($doFrontend) {
    Write-Step "Starting frontend (Vite) on http://127.0.0.1:$FrontendPort"
    $frontendCmd = "npm run dev -- --port $FrontendPort"
    Start-InNewWindow -Title "EMA Frontend :$FrontendPort" -WorkDir $FrontendDir -Command $frontendCmd
}

Write-Host ""
Write-Step "Up and running"
if ($doBackend)  { Write-Host "    API docs : http://127.0.0.1:$BackendPort/docs" -ForegroundColor Green }
if ($doFrontend) { Write-Host "    Dashboard: http://127.0.0.1:$FrontendPort" -ForegroundColor Green }
Write-Host "    (Each server runs in its own window. Close the window or run '.\start.ps1 -Stop' to stop.)" -ForegroundColor DarkGray

if ($Open -and $doFrontend) {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:$FrontendPort"
}
