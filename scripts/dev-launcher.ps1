param(
    [string]$Storage = 'K:\GeWuTest\Data',
    [string]$Cache = 'K:\GeWuTest\Cache',
    [int]$Port = 8895,
    [string]$Build = '',
    [switch]$Stop,
    [switch]$Acceptance,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'

# Fixed entry for development builds. It reads the current pointer, starts that build directly
# (no installer, no upgrade step) and refuses to attach to the storage the installed product
# uses unless -Acceptance says this run is a formal acceptance run.

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildsRoot = Join-Path $projectRoot 'builds'

if ($Build) {
    $directory = $Build
} else {
    $pointer = Join-Path $buildsRoot 'current.json'
    if (-not (Test-Path -LiteralPath $pointer)) {
        throw "No current pointer yet: $pointer. Run scripts\build-test.ps1 first."
    }
    $directory = (Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json).current
}
$buildDir = if ([IO.Path]::IsPathRooted($directory)) { $directory } else { Join-Path $buildsRoot $directory }
$appDir = Join-Path $buildDir 'app'
$exe = Join-Path $appDir 'Workbench.exe'
$manifestPath = Join-Path $appDir 'build-manifest.json'
if (-not (Test-Path -LiteralPath $exe)) { throw "This build has no Workbench.exe: $exe" }
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "This build has no build-manifest.json: $manifestPath (the only source of program identity)" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$has = { param($name) $manifest.PSObject.Properties.Name -contains $name }
if (-not $manifest.commit -or $manifest.dirty -ne $false -or $manifest.build_mode -ne 'dev' -or
    -not (Test-Path -LiteralPath (Join-Path $appDir 'backend\external_effects.py')) -or
    -not (Select-String -LiteralPath (Join-Path $appDir 'backend\app.py') -Pattern '/api/runtime/status' -Quiet)) {
    throw 'This TEST entry requires a clean build with offline policy and runtime-status support. Build a new frozen TEST candidate first.'
}

$storagePath = [IO.Path]::GetFullPath($Storage)
$cachePath = if ($Cache) { [IO.Path]::GetFullPath($Cache) } else { Join-Path (Split-Path -Parent $storagePath) 'Cache-Test' }

# The installed product records its own storage in desktop.json. Read it read-only, purely as a
# deny list, so a development launch can never attach to the delivered data directory.
$installedStorage = ''
$installedSettings = Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\desktop.json'
if (Test-Path -LiteralPath $installedSettings) {
    # Fail closed: when this file exists we cannot prove the target is not the installed product's
    # data directory, so an unreadable value refuses the launch instead of dropping the guard.
    try { $installedData = (Get-Content -LiteralPath $installedSettings -Raw | ConvertFrom-Json).data }
    catch { throw "Cannot read the installed product's settings at $installedSettings ($($_.Exception.Message)). Refusing to guess which data directory is safe." }
    if (-not $installedData) { throw "The installed product's settings at $installedSettings name no data directory. Refusing to guess which data directory is safe." }
    $installedStorage = [IO.Path]::GetFullPath($installedData)
}
if ($installedStorage -and $storagePath -eq $installedStorage -and -not $Acceptance) {
    throw "Refusing to open the installed product's data directory in development mode: $storagePath. For a formal acceptance run pass -Acceptance explicitly, after confirming the installed instance is stopped."
}
if ($Acceptance) {
    Write-Output '========================================================'
    Write-Output 'ACCEPTANCE RUN - this process uses an explicitly named data'
    Write-Output 'directory and may read or write real business data.'
    Write-Output "storage = $storagePath"
    Write-Output '========================================================'
}

$port = if ($Port -gt 0) { $Port } else {
    # Derived from the storage path so a development instance and the installed product never
    # compete for the same port; 8765 stays reserved for the installed product.
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($storagePath.ToLowerInvariant()))
    8766 + ([int]$hash[0] % 40)
}
$baseUrl = "http://127.0.0.1:$port"

function Get-Health {
    try { return Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2 } catch { return $null }
}
function Test-PortOpen {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if ($Stop) {
    if (-not (Test-PortOpen)) { Write-Output "Nothing listens on port $port; nothing to stop."; exit 0 }
    & $exe --stop --data $storagePath --cache $cachePath --port "$port" --no-open | Out-Null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (-not (Test-PortOpen)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Test-PortOpen) { throw "The instance did not stop; port $port is still in use." }
    Write-Output "Stopped: $storagePath (port $port released)"
    exit 0
}

if (-not (Test-Path -LiteralPath $storagePath)) {
    Write-Output "Data directory does not exist yet; it will be created: $storagePath"
}
if (-not (Test-Path -LiteralPath (Join-Path $storagePath 'providers'))) {
    Write-Output "Note: $storagePath\providers is missing - Agent sessions need model services configured for this data directory first."
}

Write-Output "build    : $directory"
Write-Output "build_id : $($manifest.build_id)"
Write-Output "commit   : $($manifest.commit)"
if (& $has 'branch') { Write-Output "branch   : $($manifest.branch)" }
if (& $has 'build_mode') { Write-Output "mode     : $($manifest.build_mode)$(if ($manifest.dirty) { ' / dirty worktree' })" }
Write-Output "storage  : $storagePath"
Write-Output "cache    : $cachePath"
Write-Output "port     : $port"

$existing = Get-Health
if ($existing -and $existing.application -eq 'product-tagging-workbench') {
    $existingStorage = [IO.Path]::GetFullPath($existing.storage)
    if ($existingStorage -ne $storagePath) {
        throw "Port $port already belongs to another instance (storage = $existingStorage). Use another port, or stop that instance first."
    }
    if ($existing.commit -ne $manifest.commit -or $existing.build_id -ne $manifest.build_id) {
        throw "Data directory $storagePath is still used by another version (running $($existing.build_id), this build $($manifest.build_id)). Stop that instance first so the upgrade can back up safely."
    }
    $runtime = $null
    try { $runtime = Invoke-RestMethod -Uri "$baseUrl/api/runtime/status" -TimeoutSec 2 } catch { }
    if (-not $runtime -or $runtime.profile -ne 'test' -or $runtime.external_effects.policy -ne 'offline') {
        throw 'The listener does not prove it is an offline TEST instance. Nothing was stopped or adopted.'
    }
    Write-Output "Already running: $baseUrl"
} else {
    $launchArgs = @('--data', $storagePath, '--cache', $cachePath, '--port', "$port", '--no-open')
    $env:GEWU_PROFILE = 'test'
    Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
    Start-Process -FilePath $exe -ArgumentList $launchArgs -WorkingDirectory $appDir | Out-Null
    $health = $null
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        Start-Sleep -Milliseconds 500
        $health = Get-Health
        if ($health -and $health.application -eq 'product-tagging-workbench') { break }
    }
    if (-not $health -or $health.application -ne 'product-tagging-workbench') {
        throw "Start failed. Log: $storagePath\logs\desktop.log"
    }
    # Default-deny identity check: the running process must be the build the pointer claims.
    $actualStorage = [IO.Path]::GetFullPath($health.storage)
    if ($actualStorage -ne $storagePath) { throw "storage mismatch: running $actualStorage, expected $storagePath." }
    if ($health.build_id -ne $manifest.build_id) { throw "build_id mismatch: running $($health.build_id), expected $($manifest.build_id)." }
    if ($health.commit -ne $manifest.commit) { throw "commit mismatch: running $($health.commit), expected $($manifest.commit)." }
    Write-Output "Started: $baseUrl (build_id $($health.build_id)$(if ($health.dirty) { ' / dirty worktree' }))"
}

if (-not $NoOpen) { Start-Process $baseUrl }
