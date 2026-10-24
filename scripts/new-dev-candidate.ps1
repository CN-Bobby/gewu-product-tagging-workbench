[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json')
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
$expectedConfig = Join-Path $runCenterRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfig) { throw "Run Center config is pinned to $expectedConfig." }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([IO.Path]::GetFullPath([string]$config.projectRoot).TrimEnd('\') -ine $sourceRoot) {
    throw 'DEV candidate source root must match this configured source checkout.'
}
if ([IO.Path]::GetFullPath([string]$config.profiles.DEV.data).TrimEnd('\') -ine 'K:\GeWuDev\Data' -or
    [IO.Path]::GetFullPath([string]$config.profiles.DEV.cache).TrimEnd('\') -ine 'K:\GeWuDev\Cache') {
    throw 'DEV data/cache paths differ from the approved isolated DEV locations.'
}
if ([IO.Path]::GetFullPath([string]$config.profiles.DEV.reports).TrimEnd('\') -ine 'K:\GeWuDev\Reports') {
    throw 'DEV reports must use K:\GeWuDev\Reports; TEST reports remain untouched.'
}
$reportsRoot = 'K:\GeWuDev\Reports'
$buildsRoot = Join-Path $sourceRoot 'builds'
New-Item -ItemType Directory -Force -Path $reportsRoot | Out-Null
Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
Assert-DgPlainPath $reportsRoot 'DEV reports root' -Directory | Out-Null
$lockPath = Join-Path $runCenterRoot 'maintenance.lock'
$lock = $null
try {
    try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another Run Center or maintenance operation is active; no candidate was built.' }
    $status = @(& git -C $sourceRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the source worktree.' }
    if ($status.Count -gt 0) { throw 'DEV candidate requires a clean Git worktree.' }
    $commit = (& git -C $sourceRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Could not identify a full source commit.' }
    $versionDoc = Get-Content -LiteralPath (Join-Path $sourceRoot 'VERSION.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$versionDoc.version
    if ($version -notmatch '^0\.5\.0-beta\.[1-9][0-9]*$' -or $versionDoc.release_status -ne 'pending_acceptance') {
        throw 'Only the current pending_acceptance beta version can be packaged as a DEV candidate.'
    }
    $short = $commit.Substring(0, 12)
    $candidateDir = Join-Path $buildsRoot "$version-candidate.$short"
    $reportPath = Join-Path $reportsRoot "candidate-$version-$short.dev.json"
    if (Test-Path -LiteralPath $candidateDir) { throw "Candidate build directory already exists; refusing overwrite: $candidateDir" }
    if (Test-Path -LiteralPath $reportPath) { throw "Candidate report already exists; refusing overwrite: $reportPath" }

    $python = Join-Path $sourceRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Project Python is missing: $python" }
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $offlineOutput = @(& $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run-offline-checks.ps1') -ProjectRoot $sourceRoot -ReportDir $reportsRoot 2>&1)
    $offlineExit = $LASTEXITCODE
    if ($offlineExit -ne 0) { $offlineOutput | ForEach-Object { Write-Output $_ }; throw 'Offline automated checks failed; no candidate build was created.' }
    $reportLine = @($offlineOutput | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^Offline report: (.+)$' }) | Select-Object -Last 1
    if (-not $reportLine) { throw 'Offline checks did not identify their report path.' }
    $offlineReportMatch = [regex]::Match([string]$reportLine, '^Offline report: (.+)$')
    $offlineReportPath = $offlineReportMatch.Groups[1].Value
    $offlineReportPath = Get-DgCanonicalExistingPath $offlineReportPath 'Offline check report'
    $null = Assert-DgUnderRoot $offlineReportPath $reportsRoot 'Offline check report'
    $offline = Read-DgJson $offlineReportPath 'Offline check report'
    if ($offline.status -ne 'passed' -or $offline.profile -ne 'test' -or $offline.external_requests -ne 0 -or
        $offline.dirty -ne $false -or ([string]$offline.commit).ToLowerInvariant() -ne $commit) {
        throw 'Offline report must be a passing, clean, zero-external-request run for this commit.'
    }
    & $python (Join-Path $sourceRoot 'scripts\build_windows.py') --portable
    if ($LASTEXITCODE -ne 0) { throw 'Portable candidate build failed.' }
    if (-not (Test-Path -LiteralPath $candidateDir -PathType Container)) { throw "Portable build did not create the expected immutable directory: $candidateDir" }
    $candidateApp = Join-Path $candidateDir 'app'
    $manifestPath = Join-Path $candidateApp 'build-manifest.json'
    $manifest = Read-DgJson $manifestPath 'Candidate manifest'
    if ($manifest.commit -ne $commit -or $manifest.version -ne $version -or $manifest.dirty -ne $false -or
        $manifest.build_mode -ne 'candidate' -or $manifest.storage_mode -ne 'user-selected') {
        throw 'Built manifest is not the expected clean portable candidate.'
    }
    $portableZip = Join-Path $candidateDir "GeWuWorkbench-$version-portable.zip"
    $portableSidecar = "$portableZip.sha256"
    $manifestHash = Get-DgSha256 $manifestPath
    $report = [ordered]@{
        schema = 1; status = 'built_pending_dev_acceptance'; profile = 'dev';
        created_at = [DateTime]::UtcNow.ToString('o'); version = $version; build_id = [string]$manifest.build_id
        commit = $commit; dirty = $false; source_sha256 = [string]$manifest.source_sha256
        candidate_app = $candidateApp; manifest_sha256 = $manifestHash; build_manifest_sha256 = $manifestHash
        portable_zip = $portableZip; portable_sha256 = Get-DgSha256 $portableZip
        portable_sidecar = $portableSidecar; portable_sidecar_sha256 = Get-DgSha256 $portableSidecar
        automated_report = $offlineReportPath; automated_report_sha256 = Get-DgSha256 $offlineReportPath
    }
    $reportJson = ConvertTo-Json -InputObject $report -Depth 8
    $reportStream = [IO.File]::Open($reportPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($reportJson + "`r`n")
        $reportStream.Write($bytes, 0, $bytes.Length); $reportStream.Flush($true)
    } finally { $reportStream.Dispose() }
    Test-DgCandidateBundle $reportPath $reportsRoot $buildsRoot | Out-Null
    Write-Output "DEV candidate is pending manual acceptance: $($manifest.build_id)"
    Write-Output "Candidate report: $reportPath (SHA-256 $(Get-DgSha256 $reportPath))"
    Write-Output "Portable ZIP: $portableZip (SHA-256 $($report.portable_sha256))"
    Write-Output 'The build is not frozen as accepted, promoted to PROD, or published.'
} finally {
    if ($lock) { $lock.Dispose() }
}
