param([int]$Port = 8765, [string]$Storage = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'workbench-health.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$storagePath = if ($Storage) { [IO.Path]::GetFullPath($Storage) } else { Join-Path $projectRoot 'storage' }
$baseUrl = "http://127.0.0.1:$Port"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) { Write-Output 'Workbench is not running.'; exit 0 }
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
if (-not $serverProcess.CommandLine -or -not $serverProcess.CommandLine.Contains($pythonPath) -or -not $serverProcess.CommandLine.Contains('backend.app:app')) {
    throw "Port $Port is occupied by another process; nothing was stopped."
}
$health = Get-WorkbenchHealth -BaseUrl $baseUrl -TimeoutSec 4
$legacy = -not $health.application -and $health.version -eq '0.4.0' -and -not $Storage -and $Port -eq 8765
if (-not $legacy -and ($health.application -ne 'product-tagging-workbench' -or $health.storage -ne $storagePath)) { throw 'Service identity or data directory does not match; nothing was stopped.' }
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/server/stop" -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if (-not (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $serverProcess.ProcessId -ErrorAction SilentlyContinue) { Stop-Process -Id $serverProcess.ProcessId }
$parentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($serverProcess.ParentProcessId)" -ErrorAction SilentlyContinue
if ($parentProcess -and $parentProcess.CommandLine -and $parentProcess.CommandLine.Contains($pythonPath) -and $parentProcess.CommandLine.Contains('backend.app:app')) { Stop-Process -Id $parentProcess.ProcessId -ErrorAction SilentlyContinue }
Write-Output 'Workbench stopped. Interrupted jobs can resume after the next start.'
