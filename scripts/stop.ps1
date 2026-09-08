$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) { Write-Output 'Workbench is not running.'; exit 0 }
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
if (-not $serverProcess.CommandLine.Contains($pythonPath) -or -not $serverProcess.CommandLine.Contains('backend.app:app')) {
    throw 'Port 8765 is occupied by another process; nothing was stopped.'
}
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/server/stop' -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if (-not (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue) { Stop-Process -Id $serverProcess.ProcessId }
$parentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($serverProcess.ParentProcessId)" -ErrorAction SilentlyContinue
if ($parentProcess -and $parentProcess.CommandLine -and $parentProcess.CommandLine.Contains($pythonPath) -and $parentProcess.CommandLine.Contains('backend.app:app')) { Stop-Process -Id $parentProcess.ProcessId -ErrorAction SilentlyContinue }
Write-Output 'Workbench stopped. Interrupted jobs can resume after the next start.'
