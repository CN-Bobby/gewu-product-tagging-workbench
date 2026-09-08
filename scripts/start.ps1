param([int]$Port = 8765, [string]$Storage = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$storagePath = if ($Storage) { [IO.Path]::GetFullPath($Storage) } else { Join-Path $projectRoot 'storage' }
$logRoot = Join-Path $storagePath 'logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$baseUrl = "http://127.0.0.1:$Port"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 5
    if ($health.status -ne 'ok') { throw "Port $Port is occupied by an unhealthy service." }
    Write-Output "Workbench ready: $baseUrl (version $($health.version))"
    exit 0
}
$env:PYTHONUTF8 = '1'
$env:TAG_STORAGE = $storagePath
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Python environment is missing: $pythonPath" }
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'server.out.log') -RedirectStandardError (Join-Path $logRoot 'server.err.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $logRoot 'server.pid')
$ready = $false
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 1
        if ($health.status -eq 'ok') { $ready = $true; break }
    } catch { }
    $process.Refresh()
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 300
}
if (-not $ready) {
    Get-Content -LiteralPath (Join-Path $logRoot 'server.err.log') -Tail 30
    throw "Workbench failed to start. Logs: $logRoot"
}
Write-Output "Workbench ready: $baseUrl (version $($health.version))"
