<#
.SYNOPSIS
  Start the Schema backend (demo mode) and the Vite dev server together. Ctrl+C stops both.

.DESCRIPTION
  Backend: backend\.venv\Scripts\python.exe -m schema_app on http://127.0.0.1:8420 with
  SCHEMA_DEMO=1 (fixtures provider, sample library under backend\data\demo).
  Frontend: npm run dev on http://127.0.0.1:5173, proxying /api (and the WebSocket) to the backend.

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -NoDemo -Port 8421
  .\scripts\dev.ps1 -Install        # (re)install frontend packages first
#>
[CmdletBinding()]
param(
    [switch]$NoDemo,
    [switch]$Install,
    [int]$Port = 8420
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "No virtualenv at backend\.venv. Create it with:`n  cd backend; python -m venv .venv; .venv\Scripts\python -m pip install -e '.[dev]'"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw 'npm was not found. Install Node.js 20 or newer.'
}
if ($Install -or -not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Push-Location $frontend
    try { npm ci } finally { Pop-Location }
}

if ($NoDemo) { $env:SCHEMA_DEMO = '0' } else { $env:SCHEMA_DEMO = '1' }
$env:SCHEMA_HOST = '127.0.0.1'
$env:SCHEMA_PORT = "$Port"
$env:SCHEMA_BACKEND = "http://127.0.0.1:$Port"   # read by frontend/vite.config.ts

Write-Host "Schema backend  -> http://127.0.0.1:$Port  (demo: $env:SCHEMA_DEMO)"
Write-Host 'Vite dev server -> http://127.0.0.1:5173'
$backendProc = Start-Process -FilePath $python -ArgumentList @('-m', 'schema_app') `
    -WorkingDirectory $backend -NoNewWindow -PassThru

try {
    Start-Sleep -Milliseconds 500
    if ($backendProc.HasExited) { throw "The backend exited immediately (code $($backendProc.ExitCode))." }
    Push-Location $frontend
    npm run dev
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
    if ($backendProc -and -not $backendProc.HasExited) {
        # taskkill /T also stops anything uvicorn spawned.
        & taskkill.exe /PID $backendProc.Id /T /F | Out-Null
    }
    Write-Host 'Stopped.'
}
