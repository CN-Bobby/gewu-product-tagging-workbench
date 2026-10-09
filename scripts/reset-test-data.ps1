[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$FixtureRoot = 'K:\GeWuFixtures\project-library-v1',
    [string]$Reports = 'K:\GeWuTest\Reports',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'))
$expectedConfigPath = Join-Path $runCenterRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfigPath) {
    throw "Run Center config is pinned to $expectedConfigPath so all TEST maintenance actions share one lock."
}

function Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Assert-Within([string]$Path, [string]$Root) {
    $candidate = Canonical $Path
    $base = Canonical $Root
    if ($candidate -ne $base -and -not $candidate.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes approved root: $candidate"
    }
    return $candidate
}
function Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$target = Canonical $config.profiles.TEST.data
$cache = Canonical $config.profiles.TEST.cache
$app = Canonical $config.profiles.TEST.app
$fixture = Canonical $FixtureRoot
$reports = Canonical $Reports
$null = Assert-Within $target 'K:\GeWuTest'
if ($target -ne 'K:\GeWuTest\Data') { throw "TEST data path is not the approved fixed path: $target" }
if ($cache -ne 'K:\GeWuTest\Cache' -or $app -ne 'K:\GeWuTest\Current\app') { throw 'TEST app/cache paths do not match the fixed K:\GeWuTest layout.' }
if ($reports -ne 'K:\GeWuTest\Reports' -or (Canonical $config.profiles.TEST.reports) -ne $reports) { throw 'TEST reports are pinned to K:\GeWuTest\Reports.' }
if ($cache -eq $target -or $cache.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $target.StartsWith($cache + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'TEST data and cache paths overlap.' }
if ($reports -eq $target -or $reports.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $target.StartsWith($reports + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Test reports must stay outside resettable TEST data.' }
if (-not (Test-Path -LiteralPath $fixture -PathType Container)) { throw "Fixture package is missing: $fixture" }
$manifestPath = Join-Path $fixture 'fixture-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Fixture manifest is missing.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.fixture_version -ne 'project-library-v1' -or -not $manifest.files -or $manifest.files.Count -lt 2) {
    throw 'Fixture version or inventory is invalid.'
}
$seed = Join-Path $fixture 'seed-data'
$expected = @{}
foreach ($item in $manifest.files) {
    $relative = [string]$item.path
    if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)' -or
        $relative -match '(?i)(^|[\\/])(\.env|credentials\.env|settings\.json)$|(^|[\\/])(logs|backups)([\\/]|$)') {
        throw "Fixture contains a forbidden or unsafe path: $relative"
    }
    $full = Canonical (Join-Path $fixture $relative)
    $null = Assert-Within $full $seed
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Fixture file is missing: $relative" }
    if ((Sha256 $full) -ne ([string]$item.sha256).ToLowerInvariant()) { throw "Fixture hash mismatch: $relative" }
    $expected[$full] = $true
}
$actual = @(Get-ChildItem -LiteralPath $seed -Recurse -File -Force | ForEach-Object { Canonical $_.FullName })
if ($actual.Count -ne $expected.Count -or @($actual | Where-Object { -not $expected.ContainsKey($_) }).Count -gt 0) {
    throw 'Fixture seed-data contains files not listed in its SHA-256 manifest.'
}
$manifestHash = Sha256 $manifestPath
$stage = $target + '.reset-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$legacyRoot = Join-Path (Split-Path -Parent $target) 'Backups'
$legacy = Join-Path $legacyRoot ('Data-before-reset-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$listener = Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue
Write-Output 'TEST data reset target:'
Write-Output "  current: $target"
Write-Output "  preserved as: $legacy"
Write-Output "  fixture: $fixture (manifest SHA-256 $manifestHash)"
Write-Output "  reports: $reports"
Write-Output 'No PROD data or credentials are included in the fixture. Legacy TEST data remains preserved for manual review.'
if (-not $Apply) {
    Write-Output 'Preview only. Pass -Apply to stop the registered TEST instance, preserve its current data directory, and seed the fixed fixture.'
    exit 0
}

$lockPath = Join-Path $runCenterRoot 'maintenance.lock'
try { $maintenanceLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
catch { throw 'Another Run Center start or TEST maintenance operation is active. Nothing was reset.' }
$listener = Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue

if ($listener) {
    $runner = Join-Path $runCenterRoot 'run-center.ps1'
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Run Center script is missing: $runner" }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -Profile TEST -Action Stop -ConfigPath $ConfigPath -NoOpen
    if ($LASTEXITCODE -ne 0) { throw 'Run Center could not stop the registered TEST instance.' }
}
$listener = Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw "TEST port $($config.profiles.TEST.port) still has a listener; nothing was reset." }

if (Test-Path -LiteralPath $stage) { throw "Staging path already exists: $stage" }
if (Test-Path -LiteralPath $legacy) { throw "Preservation path already exists: $legacy" }
New-Item -ItemType Directory -Force -Path $stage, $legacyRoot, $reports | Out-Null
try {
    Get-ChildItem -LiteralPath $seed -Force | Copy-Item -Destination $stage -Recurse -Force
    $stagedFiles = @(Get-ChildItem -LiteralPath $stage -Recurse -File -Force)
    if ($stagedFiles.Count -ne $expected.Count) { throw 'Staged fixture file count does not match the manifest.' }
    foreach ($item in $manifest.files) {
        $staged = Join-Path $stage ([string]$item.path).Substring('seed-data/'.Length).Replace('/', '\')
        if ((Sha256 $staged) -ne ([string]$item.sha256).ToLowerInvariant()) { throw "Staged fixture hash mismatch: $($item.path)" }
    }
    if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $legacy }
    try { Move-Item -LiteralPath $stage -Destination $target }
    catch {
        if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $legacy)) { Move-Item -LiteralPath $legacy -Destination $target }
        throw
    }
} catch {
    if (Test-Path -LiteralPath $stage) { Move-Item -LiteralPath $stage -Destination ($stage + '.failed') }
    throw
}
$result = [ordered]@{ schema = 1; created_at = [DateTime]::UtcNow.ToString('o'); action = 'test-data-reset';
    status = 'completed'; fixture_version = $manifest.fixture_version; fixture_manifest_sha256 = $manifestHash;
    target = $target; preserved_legacy_data = if (Test-Path -LiteralPath $legacy) { $legacy } else { $null };
    reports = $reports; project_count = $manifest.projects.Count; external_calls = 0 }
$reportPath = Join-Path $reports ("test-data-reset-$((Get-Date).ToString('yyyyMMdd-HHmmss')).json")
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Output "TEST data restored from $($manifest.fixture_version). Report: $reportPath"
