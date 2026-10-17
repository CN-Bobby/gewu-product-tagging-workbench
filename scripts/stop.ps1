param([int]$Port = 8896, [string]$Storage = 'K:\GeWuDev\Data')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'workbench-health.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$storagePath = [IO.Path]::GetFullPath($Storage)
$baseUrl = "http://127.0.0.1:$Port"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) { Write-Output 'Workbench is not running.'; exit 0 }
$pidPath = Join-Path $storagePath 'logs\server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { throw "No launcher-owned process record exists at $pidPath; nothing was stopped." }
$recordedPid = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$recordedPid)) { throw "Invalid process record at $pidPath; nothing was stopped." }
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$recordedPid" -ErrorAction SilentlyContinue
if (-not $serverProcess -or -not $serverProcess.CommandLine -or -not $serverProcess.CommandLine.Contains($pythonPath) -or -not $serverProcess.CommandLine.Contains('backend.app:app')) {
    throw "Recorded process identity does not match the DEV backend; nothing was stopped."
}
$listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
if (-not $listenerProcess -or (-not $listenerProcess.CommandLine.Contains('backend.app:app') -and $listenerProcess.ParentProcessId -ne $recordedPid)) {
    throw "Port $Port is not served by the recorded DEV backend; nothing was stopped."
}
$health = Get-WorkbenchHealth -BaseUrl $baseUrl -TimeoutSec 4
$legacy = -not $health.application -and $health.version -eq '0.4.0' -and -not $Storage -and $Port -eq 8765
if (-not $legacy -and ($health.application -ne 'product-tagging-workbench' -or $health.storage -ne $storagePath)) { throw 'Service identity or data directory does not match; nothing was stopped.' }
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/server/stop" -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if (-not (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue) { throw "The recorded DEV backend did not exit after its stop request; no process was force-terminated." }
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
Write-Output 'Workbench stopped. Interrupted jobs can resume after the next start.'
