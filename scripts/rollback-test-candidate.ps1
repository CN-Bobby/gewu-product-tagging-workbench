[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BuildId,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [switch]$RestoreData
)
$ErrorActionPreference = 'Stop'
$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'))
$expectedConfigPath = Join-Path $runCenterRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfigPath) {
    throw "Run Center config is pinned to $expectedConfigPath so all TEST maintenance actions share one lock."
}
function Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Verify-App($AppPath) {
    $manifestPath = Join-Path $AppPath 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Build manifest is missing: $AppPath" }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $manifest.files) { throw "Build manifest has no file inventory: $manifestPath" }
    $expected = @($manifest.files.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($relative in $expected) {
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { throw "Unsafe manifest path: $relative" }
        $path = Join-Path $AppPath $relative.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Sha256 $path) -ne ([string]$manifest.files.$relative).ToLowerInvariant()) {
            throw "App file hash mismatch: $relative"
        }
    }
    $files = @(Get-ChildItem -LiteralPath $AppPath -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($AppPath.Length).TrimStart('\').Replace('\', '/')
    } | Where-Object { $_ -ne 'build-manifest.json' })
    if ($files.Count -ne $expected.Count -or @($files | Where-Object { $_ -notin $expected }).Count -gt 0) { throw 'App contains unlisted files.' }
    return $manifest
}
function TreeHash([string]$Root) {
    $base = Canonical $Root
    $blocked = @('.env', 'credentials.env', 'settings.json')
    $rows = @(Get-ChildItem -LiteralPath $base -Recurse -File -Force | Where-Object {
        $rel = $_.FullName.Substring($base.Length).TrimStart('\')
        $_.Name -notin $blocked -and $_.Extension -ne '.log' -and $rel -notmatch '(?i)(^|\\)(logs|backups)(\\|$)'
    } | Sort-Object FullName)
    $text = [Text.StringBuilder]::new()
    foreach ($file in $rows) { [void]$text.Append($file.FullName.Substring($base.Length).TrimStart('\')).Append("`0").Append((Sha256 $file.FullName)).Append("`n") }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text.ToString()))).ToLowerInvariant() }
    finally { $sha.Dispose() }
    return @{ count = $rows.Count; hash = $hash }
}

if ($BuildId -notmatch '^[a-zA-Z0-9.+_-]{1,100}$') { throw 'BuildId contains unsafe path characters.' }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$lockPath = Join-Path $runCenterRoot 'maintenance.lock'
try { $maintenanceLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
catch { throw 'Another Run Center start or TEST maintenance operation is active. Nothing was rolled back.' }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$currentApp = Canonical $config.profiles.TEST.app
$data = Canonical $config.profiles.TEST.data
$cache = Canonical $config.profiles.TEST.cache
$reports = Canonical $config.profiles.TEST.reports
if ($currentApp -ne 'K:\GeWuTest\Current\app' -or $data -ne 'K:\GeWuTest\Data' -or
    $cache -ne 'K:\GeWuTest\Cache' -or $reports -ne 'K:\GeWuTest\Reports') {
    throw 'TEST app/data/cache/reports must match the fixed K:\GeWuTest layout. Nothing was changed.'
}
$currentManifest = Verify-App $currentApp
if ($currentManifest.build_id -ne $BuildId) { throw "Current TEST build ($($currentManifest.build_id)) does not match requested rollback build ($BuildId)." }
$previousApp = 'K:\GeWuTest\Rollback\Previous\app'
$previousManifest = Verify-App $previousApp
$candidateRecord = Join-Path $config.profiles.TEST.reports "candidate-$BuildId-$($currentManifest.commit.Substring(0,12)).json"
if (-not (Test-Path -LiteralPath $candidateRecord -PathType Leaf)) { throw 'Activation record is missing; refusing an untracked rollback.' }
$candidate = Get-Content -LiteralPath $candidateRecord -Raw -Encoding UTF8 | ConvertFrom-Json
$backupRoot = Join-Path 'K:\GeWuTest\Backups' $BuildId
$dataBackup = Join-Path $backupRoot 'Data'
$cacheBackup = Join-Path $backupRoot 'Cache'
if ($RestoreData -and (-not (Test-Path -LiteralPath $dataBackup -PathType Container) -or -not (Test-Path -LiteralPath $cacheBackup -PathType Container))) {
    throw 'The sanitized TEST data/cache backup is incomplete; no rollback was attempted.'
}
$listener = Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $runner = Join-Path $runCenterRoot 'run-center.ps1'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -Profile TEST -Action Stop -ConfigPath $ConfigPath -NoOpen
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the registered TEST process; nothing was moved.' }
}
if (Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue) {
    throw 'TEST port remains occupied; nothing was moved.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$failedRoot = Join-Path 'K:\GeWuTest\Rollback\Failed' "$BuildId-$stamp"
if (Test-Path -LiteralPath $failedRoot) { throw "Failed-build preservation path exists: $failedRoot" }
New-Item -ItemType Directory -Force -Path $failedRoot | Out-Null
$dataStage = $data + '.rollback-' + $stamp
$cacheStage = $cache + '.rollback-' + $stamp
if ($RestoreData) {
    $expectedDataHash = [string]$candidate.data_backup.sha256
    $expectedCacheHash = [string]$candidate.cache_backup.sha256
    if ((TreeHash $dataBackup).hash -ne $expectedDataHash -or (TreeHash $cacheBackup).hash -ne $expectedCacheHash) {
        throw 'TEST data or cache backup hash no longer matches the activation report.'
    }
    Copy-Item -LiteralPath $dataBackup -Destination $dataStage -Recurse
    Copy-Item -LiteralPath $cacheBackup -Destination $cacheStage -Recurse
    if ((TreeHash $dataStage).hash -ne $expectedDataHash -or (TreeHash $cacheStage).hash -ne $expectedCacheHash) {
        throw 'Staged rollback data/cache hash mismatch. Current directories were not moved.'
    }
}

$currentMoved = $false; $previousMoved = $false; $dataMoved = $false; $cacheMoved = $false
try {
    Move-Item -LiteralPath $currentApp -Destination (Join-Path $failedRoot 'app')
    $currentMoved = $true
    Move-Item -LiteralPath $previousApp -Destination $currentApp
    $previousMoved = $true
    if ($RestoreData) {
        Move-Item -LiteralPath $data -Destination (Join-Path $failedRoot 'Data')
        $dataMoved = $true
        Move-Item -LiteralPath $dataStage -Destination $data
        Move-Item -LiteralPath $cache -Destination (Join-Path $failedRoot 'Cache')
        $cacheMoved = $true
        Move-Item -LiteralPath $cacheStage -Destination $cache
    }
} catch {
    if ($cacheMoved -and (Test-Path -LiteralPath $cache)) { Move-Item -LiteralPath $cache -Destination (Join-Path $failedRoot 'rollback-new-Cache') }
    if ($cacheMoved -and (Test-Path -LiteralPath (Join-Path $failedRoot 'Cache'))) { Move-Item -LiteralPath (Join-Path $failedRoot 'Cache') -Destination $cache }
    if ($dataMoved -and (Test-Path -LiteralPath $data)) { Move-Item -LiteralPath $data -Destination (Join-Path $failedRoot 'rollback-new-Data') }
    if ($dataMoved -and (Test-Path -LiteralPath (Join-Path $failedRoot 'Data'))) { Move-Item -LiteralPath (Join-Path $failedRoot 'Data') -Destination $data }
    if ($previousMoved -and (Test-Path -LiteralPath $currentApp)) { Move-Item -LiteralPath $currentApp -Destination $previousApp }
    if ($currentMoved -and (Test-Path -LiteralPath (Join-Path $failedRoot 'app'))) { Move-Item -LiteralPath (Join-Path $failedRoot 'app') -Destination $currentApp }
    throw
}
$report = [ordered]@{
    schema = 1; created_at = [DateTime]::UtcNow.ToString('o'); action = 'rollback-test-candidate'
    status = 'restored_previous_pending_identity_check'; failed_build_id = $BuildId
    restored_build_id = $previousManifest.build_id; restored_commit = $previousManifest.commit
    restored_offline_compatible = [bool]((Test-Path -LiteralPath (Join-Path $currentApp 'backend\external_effects.py')) -and
        (Select-String -LiteralPath (Join-Path $currentApp 'backend\app.py') -Pattern '/api/runtime/status' -Quiet))
    data_restored = [bool]$RestoreData; data_path = $data; cache_path = $cache
    preserved_failed_candidate = $failedRoot; activation_record = $candidateRecord
    external_calls = 0
}
$reportPath = Join-Path $reports "rollback-$BuildId-$stamp.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Output "TEST rollback completed: $BuildId -> $($previousManifest.build_id)"
Write-Output "Failed program preserved at: $failedRoot"
Write-Output "Previous runtime is offline-compatible: $($report.restored_offline_compatible)"
if (-not $report.restored_offline_compatible) { Write-Output 'Run Center will refuse to start this previous build until it passes the current offline runtime gate.' }
Write-Output "Rollback report: $reportPath"
