param([int]$Port = 8896, [string]$Storage = 'K:\GeWuDev\Data', [string]$Cache = 'K:\GeWuDev\Cache')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'workbench-health.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$storagePath = [IO.Path]::GetFullPath($Storage)
$cachePath = [IO.Path]::GetFullPath($Cache)
$logRoot = Join-Path $storagePath 'logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$baseUrl = "http://127.0.0.1:$Port"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $health = Get-WorkbenchHealth -BaseUrl $baseUrl -TimeoutSec 5
    $matchingService = $health.application -eq 'product-tagging-workbench' -and $health.storage -eq $storagePath
    if (-not $health.application -and $health.version -eq '0.4.0' -and -not $Storage -and $Port -eq 8765) {
        $legacyProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $matchingService = $legacyProcess.CommandLine -and $legacyProcess.CommandLine.Contains($pythonPath) -and $legacyProcess.CommandLine.Contains('backend.app:app')
    }
    if ($health.status -ne 'ok' -or -not $matchingService) { throw "Port $Port belongs to another service or data directory. Choose another port; nothing was stopped." }
    Write-Output "Workbench ready: $baseUrl (version $($health.version))"
    exit 0
}
$env:PYTHONUTF8 = '1'
$env:TAG_STORAGE = $storagePath
$env:TAG_CACHE = $cachePath
$env:GEWU_PROFILE = 'dev'
$env:TAG_PROVIDER_HOME = Join-Path $storagePath 'providers'
$env:GEWU_API_URL = $baseUrl
$env:GEWU_FRONTEND_URL = "http://127.0.0.1:5174"
$env:GEWU_INSTANCE_ID = 'DEV'
Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
Remove-Item Env:GEWU_TEST_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Python environment is missing: $pythonPath" }
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$Port",'--reload') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'server.out.log') -RedirectStandardError (Join-Path $logRoot 'server.err.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $logRoot 'server.pid')
$ready = $false
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    try {
        $health = Get-WorkbenchHealth -BaseUrl $baseUrl -TimeoutSec 1
        if ($health.status -eq 'ok' -and $health.application -eq 'product-tagging-workbench' -and $health.storage -eq $storagePath) { $ready = $true; break }
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
