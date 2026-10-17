[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$ReportDir = 'K:\GeWuTest\Reports',
    [string]$CurrentRoot = 'K:\GeWuTest\Current',
    [string]$FixtureManifest = 'K:\GeWuFixtures\project-library-v1\fixture-manifest.json',
    [switch]$Activate,
    [switch]$StartAfter
)
$ErrorActionPreference = 'Stop'
$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'))
$expectedConfigPath = Join-Path $runCenterRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfigPath) {
    throw "Run Center config is pinned to $expectedConfigPath so all TEST maintenance actions share one lock."
}

function Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Assert-Within([string]$Path, [string]$Root) {
    $candidate = Canonical $Path; $base = Canonical $Root
    if ($candidate -ne $base -and -not $candidate.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes approved root: $candidate"
    }
    return $candidate
}
function Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Save-CandidateReport($Report, [string]$Path) {
    $temporary = $Path + '.tmp'
    $Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}
function Test-ManifestFiles([string]$AppPath, $Manifest) {
    $expected = @($Manifest.files.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($relative in $expected) {
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { throw "Unsafe build manifest path: $relative" }
        $path = Canonical (Join-Path $AppPath $relative.Replace('/', '\'))
        $null = Assert-Within $path $AppPath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Sha256 $path) -ne ([string]$Manifest.files.$relative).ToLowerInvariant()) {
            throw "Build file hash does not match manifest: $relative"
        }
    }
    $actual = @(Get-ChildItem -LiteralPath $AppPath -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($AppPath.Length).TrimStart('\').Replace('\', '/')
    } | Where-Object { $_ -ne 'build-manifest.json' })
    if ($actual.Count -ne $expected.Count -or @($actual | Where-Object { $_ -notin $expected }).Count -gt 0) {
        throw 'Candidate app contains files outside the build manifest.'
    }
}
function Get-SafeTree([string]$Root) {
    $rootPath = Canonical $Root
    $excludedDirectories = @('logs', 'backups')
    $excludedFiles = @('.env', 'credentials.env', 'settings.json')
    $rows = @(Get-ChildItem -LiteralPath $rootPath -Recurse -File -Force | Where-Object {
        $relative = $_.FullName.Substring($rootPath.Length).TrimStart('\')
        $inExcludedDirectory = $false
        foreach ($directory in $excludedDirectories) {
            if ($relative -match ('(?i)(^|\\)' + [regex]::Escape($directory) + '(\\|$)')) { $inExcludedDirectory = $true; break }
        }
        $_.Name -notin $excludedFiles -and $_.Extension -ne '.log' -and -not $inExcludedDirectory
    } | Sort-Object FullName)
    $summary = [Text.StringBuilder]::new()
    foreach ($file in $rows) {
        $relative = $file.FullName.Substring($rootPath.Length).TrimStart('\')
        [void]$summary.Append($relative).Append("`0").Append((Sha256 $file.FullName)).Append("`n")
    }
    $digest = [Security.Cryptography.SHA256]::Create()
    try { $treeHash = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($summary.ToString()))).Replace('-','').ToLowerInvariant() }
    finally { $digest.Dispose() }
    return @{ rows = $rows; hash = $treeHash }
}
function Copy-SanitizedBackup([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return @{ path = $null; files = 0; sha256 = $null } }
    if (Test-Path -LiteralPath $Destination) { throw "Backup destination already exists: $Destination" }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $args = @($Source, $Destination, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1', '/XJ', '/NFL', '/NDL', '/NP', '/XF', '.env', 'credentials.env', 'settings.json', '*.log', '/XD', 'logs', 'backups')
    & robocopy.exe @args | Out-Null
    $copyCode = $LASTEXITCODE
    if ($copyCode -ge 8) { throw "Robocopy failed while preserving TEST data (code $copyCode)." }
    $sourceTree = Get-SafeTree $Source
    $backupTree = Get-SafeTree $Destination
    if ($sourceTree.hash -ne $backupTree.hash -or $sourceTree.rows.Count -ne $backupTree.rows.Count) {
        throw "TEST data backup inventory/hash verification failed: $Destination"
    }
    return @{ path = $Destination; files = $backupTree.rows.Count; sha256 = $backupTree.hash }
}

$ProjectRoot = Canonical $ProjectRoot
$Commit = $Commit.Trim().ToLowerInvariant()
if ($Commit -notmatch '^[0-9a-f]{40}$') { throw 'Commit must be a full 40-character Git commit id.' }
$head = (& git -C $ProjectRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $head -ne $Commit) { throw "Worktree HEAD ($head) does not match requested candidate commit ($Commit)." }
$dirtyLines = @(& git -C $ProjectRoot status --porcelain)
if ($dirtyLines.Count -gt 0) { throw 'TEST candidate worktree must be clean. Existing edits were left untouched.' }
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Project Python environment is missing: $python" }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\node_modules\vite\bin\vite.js'))) { throw 'Vite dependencies are missing from this fixed candidate worktree.' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'storage\build-tools\reasonix.exe'))) { throw 'Pinned build inputs are missing from this fixed candidate worktree.' }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$fixedCandidateRoot = if ($config.candidateProjectRoot) { Canonical $config.candidateProjectRoot } else { '' }
if (-not $fixedCandidateRoot -or $ProjectRoot -ne $fixedCandidateRoot) {
    throw "TEST candidates must be built from the fixed clean source worktree in Run Center config: $fixedCandidateRoot"
}
$data = Canonical $config.profiles.TEST.data
$cache = Canonical $config.profiles.TEST.cache
$expectedCurrent = Canonical $config.profiles.TEST.app
$CurrentRoot = Canonical $CurrentRoot
$ReportDir = Canonical $ReportDir
$null = Assert-Within $data 'K:\GeWuTest'
if ($data -ne 'K:\GeWuTest\Data' -or $cache -ne 'K:\GeWuTest\Cache' -or $expectedCurrent -ne (Canonical (Join-Path $CurrentRoot 'app'))) {
    throw 'Run Center TEST code/data/cache paths do not match the fixed K:\GeWuTest layout.'
}
if ($CurrentRoot -ne 'K:\GeWuTest\Current' -or $ReportDir -ne 'K:\GeWuTest\Reports' -or
    (Canonical $config.profiles.TEST.reports) -ne $ReportDir) { throw 'TEST Current and Reports are pinned to K:\GeWuTest.' }
if ($StartAfter -and -not $Activate) { throw '-StartAfter requires -Activate.' }

$reviewedReport = $null
$reviewedReportPath = $null
$commitSuffix = $Commit.Substring(0,12)
if ($Activate) {
    $sameCommitReports = @(Get-ChildItem -LiteralPath $ReportDir -Filter "candidate-*-$commitSuffix.json" -File -ErrorAction SilentlyContinue)
    if ($sameCommitReports.Count -gt 1) { throw 'Multiple candidate reports exist for this commit; resolve the report identity before activation.' }
    if ($sameCommitReports.Count -eq 1) {
        $reviewedReportPath = Canonical $sameCommitReports[0].FullName
        $reviewedReport = Get-Content -LiteralPath $reviewedReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($reviewedReport.status -ne 'built_pending_activation' -or $reviewedReport.commit -ne $Commit -or
            $reviewedReport.dirty -ne $false -or $reviewedReport.external_requests -ne 0) {
            throw 'Existing candidate report is not an untouched, clean, offline report awaiting activation.'
        }
        $reviewedCheckPath = Canonical ([string]$reviewedReport.automated_report)
        $null = Assert-Within $reviewedCheckPath $ReportDir
        if (-not (Test-Path -LiteralPath $reviewedCheckPath -PathType Leaf) -or
            (Sha256 $reviewedCheckPath) -ne ([string]$reviewedReport.automated_report_sha256).ToLowerInvariant()) {
            throw 'Reviewed offline report is missing or its SHA-256 changed; activation was refused.'
        }
        $checkReport = Get-Item -LiteralPath $reviewedCheckPath
        $checkDoc = Get-Content -LiteralPath $reviewedCheckPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($checkDoc.commit -ne $Commit -or $checkDoc.status -ne 'passed' -or $checkDoc.profile -ne 'test' -or
            $checkDoc.dirty -ne $false -or $checkDoc.external_requests -ne 0) {
            throw 'Reviewed offline report does not prove a clean offline pass for this commit.'
        }
    }
}

if (-not $reviewedReport) {
    $started = Get-Date
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot 'scripts\run-offline-checks.ps1') -ProjectRoot $ProjectRoot -ReportDir $ReportDir
    if ($LASTEXITCODE -ne 0) { throw 'Offline automated checks failed; TEST was not changed.' }
    $checkReport = Get-ChildItem -LiteralPath $ReportDir -Filter 'offline-*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $started } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $checkReport) { throw 'Offline check report was not produced.' }
    $checkDoc = Get-Content -LiteralPath $checkReport.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($checkDoc.commit -ne $Commit -or $checkDoc.status -ne 'passed' -or $checkDoc.profile -ne 'test' -or
        $checkDoc.dirty -ne $false -or $checkDoc.external_requests -ne 0) {
        throw 'Offline check report does not match a clean, offline pass for the requested commit.'
    }
}

if ($reviewedReport) {
    if (-not $reviewedReport.candidate_app) { throw 'Reviewed candidate report does not identify its immutable build directory.' }
    $candidateApp = Canonical ([string]$reviewedReport.candidate_app)
    $candidateBuildRoot = Canonical (Split-Path -Parent $candidateApp)
    $null = Assert-Within $candidateBuildRoot (Join-Path $ProjectRoot 'builds')
    if ((Split-Path -Leaf $candidateApp) -ne 'app') { throw 'Reviewed candidate app path is invalid.' }
    $manifestPath = Join-Path $candidateApp 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        (Sha256 $manifestPath) -ne ([string]$reviewedReport.manifest_sha256).ToLowerInvariant()) {
        throw 'Frozen build manifest is missing or changed since review; activation was refused.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $portablePath = Canonical ([string]$reviewedReport.portable_zip)
    $portableSidecar = Canonical ([string]$reviewedReport.portable_sidecar)
    $null = Assert-Within $portablePath $candidateBuildRoot
    $null = Assert-Within $portableSidecar $candidateBuildRoot
    if (-not (Test-Path -LiteralPath $portablePath -PathType Leaf) -or
        (Sha256 $portablePath) -ne ([string]$reviewedReport.portable_sha256).ToLowerInvariant() -or
        -not (Test-Path -LiteralPath $portableSidecar -PathType Leaf) -or
        (Sha256 $portableSidecar) -ne ([string]$reviewedReport.portable_sidecar_sha256).ToLowerInvariant()) {
        throw 'Frozen portable ZIP or checksum sidecar changed since review; activation was refused.'
    }
    $sidecarLine = (Get-Content -LiteralPath $portableSidecar -Raw -Encoding UTF8).Trim()
    if ($sidecarLine -ne "$($reviewedReport.portable_sha256)  $(Split-Path -Leaf $portablePath)") {
        throw 'Portable ZIP checksum sidecar does not match the frozen ZIP.'
    }
} else {
    $version = (Get-Content -LiteralPath (Join-Path $ProjectRoot 'VERSION.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    $candidateDirectory = "$version-candidate.$commitSuffix"
    $candidateBuildRoot = Join-Path (Join-Path $ProjectRoot 'builds') $candidateDirectory
    $candidateApp = Canonical (Join-Path $candidateBuildRoot 'app')
    $portablePath = Canonical (Join-Path $candidateBuildRoot "GeWuWorkbench-$version-portable.zip")
    $portableSidecar = $portablePath + '.sha256'
    $manifestPath = Join-Path $candidateApp 'build-manifest.json'
    if (Test-Path -LiteralPath $candidateBuildRoot) {
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $portablePath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $portableSidecar -PathType Leaf)) {
            throw "An incomplete build directory already exists and was preserved: $candidateBuildRoot"
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $sourceHashOutput = @(& $python '-c' "from backend.build_info import build_info; print(build_info()['source_sha256'])")
        if ($LASTEXITCODE -ne 0 -or $sourceHashOutput.Count -ne 1 -or
            ([string]$manifest.source_sha256) -ne ([string]$sourceHashOutput[0]).Trim()) {
            throw 'Existing build source hash does not match this clean candidate worktree; it was left untouched.'
        }
        $pinsPath = Join-Path $ProjectRoot 'build-tools.lock.json'
        $pins = Get-Content -LiteralPath $pinsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $pythonPin = [string]$pins.'python-3.14.3-embed-amd64.zip'.sha256
        $reasonixPin = [string]$pins.'reasonix.exe'.sha256
        if ($manifest.python.sha256 -ne $pythonPin -or $manifest.reasonix.sha256 -ne $reasonixPin) {
            throw 'Existing build does not use the pinned Python and Reasonix inputs; it was left untouched.'
        }
        if ($manifest.build_mode -ne 'candidate' -or $manifest.storage_mode -ne 'user-selected' -or
            (Sha256 $portablePath) -ne (Get-Content -LiteralPath $portableSidecar -Raw -Encoding UTF8).Trim().Split(' ')[0]) {
            throw 'Existing build is not a verified portable candidate; it was left untouched.'
        }
        Write-Output "Reusing the existing candidate build after source and pinned-runtime identity checks: $candidateBuildRoot"
    } else {
        $env:GEWU_PROFILE = 'test'; $env:GEWU_TEST_EXTERNAL_EFFECTS = 'offline'
        Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
        $env:TAG_STORAGE = Join-Path $env:TEMP 'GeWuCandidateBuildSandbox\Data'
        $env:TAG_CACHE = Join-Path $env:TEMP 'GeWuCandidateBuildSandbox\Cache'
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # Python packaging tools write routine progress and advisory messages to stderr.
            # Capture those lines and decide success from Python's exit code, like the offline gate.
            $ErrorActionPreference = 'Continue'
            $buildOutput = @(& $python (Join-Path $ProjectRoot 'scripts\build_windows.py') --portable 2>&1 | ForEach-Object { $_.ToString() })
            $buildExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        foreach ($line in $buildOutput) { Write-Host $line }
        if ($buildExit -ne 0) { throw "Clean TEST candidate build failed (exit $buildExit)." }
        $jsonLine = $buildOutput | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
        if (-not $jsonLine) { throw 'Build script did not return a candidate manifest summary.' }
        $build = $jsonLine | ConvertFrom-Json
        if ($build.commit -ne $Commit -or $build.dirty -ne $false -or $build.build_mode -ne 'candidate' -or
            $build.storage_mode -ne 'user-selected' -or $build.directory -ne $candidateDirectory -or
            $build.portable_sha256 -ne (Sha256 $portablePath)) {
            throw 'Build summary identity is invalid or does not match the requested clean candidate commit.'
        }
        # Rebuild the path from the local fixed worktree. Native stdout can corrupt non-ASCII paths.
        $candidateApp = Canonical (Join-Path $candidateBuildRoot 'app')
        $manifestPath = Join-Path $candidateApp 'build-manifest.json'
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
}
if ($manifest.commit -ne $Commit -or $manifest.dirty -ne $false -or $manifest.build_mode -ne 'candidate' -or $manifest.storage_mode -ne 'user-selected') {
    throw 'Candidate manifest is not a clean, commit-pinned portable candidate build.'
}
Test-ManifestFiles $candidateApp $manifest
if ($manifest.files.'backend/credential_vault.py' -and $manifest.files.'backend/external_effects.py') { } else { throw 'Candidate is missing vault or offline-policy modules.' }
if (-not (Test-Path -LiteralPath $portablePath -PathType Leaf) -or -not (Test-Path -LiteralPath $portableSidecar -PathType Leaf)) {
    throw 'Portable candidate ZIP or checksum sidecar is missing.'
}
$portableHash = Sha256 $portablePath
$sidecarLine = (Get-Content -LiteralPath $portableSidecar -Raw -Encoding UTF8).Trim()
if ($sidecarLine -ne "$portableHash  $(Split-Path -Leaf $portablePath)") { throw 'Portable ZIP checksum sidecar is invalid.' }

$fixtureVersion = 'not-available'
$fixtureHash = $null
if (Test-Path -LiteralPath $FixtureManifest -PathType Leaf) {
    $fixtureDoc = Get-Content -LiteralPath $FixtureManifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $fixtureVersion = [string]$fixtureDoc.fixture_version
    $fixtureHash = Sha256 $FixtureManifest
}
$reportPath = Join-Path $ReportDir ("candidate-$($manifest.build_id)-$($Commit.Substring(0,12)).json")
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
if ($reviewedReport) {
    if ((Canonical $reportPath) -ne $reviewedReportPath) { throw 'Rebuilt candidate build ID differs from the reviewed report; activation was refused.' }
    $sameIdentity = $reviewedReport.build_id -eq $manifest.build_id -and $reviewedReport.commit -eq $Commit -and
        $reviewedReport.source_sha256 -eq $manifest.source_sha256 -and $reviewedReport.manifest_sha256 -eq (Sha256 $manifestPath) -and
        $reviewedReport.portable_zip -eq $portablePath -and $reviewedReport.portable_sha256 -eq $portableHash -and
        $reviewedReport.portable_sidecar -eq $portableSidecar -and
        $reviewedReport.portable_sidecar_sha256 -eq (Sha256 $portableSidecar) -and
        $reviewedReport.fixture_version -eq $fixtureVersion -and $reviewedReport.fixture_manifest_sha256 -eq $fixtureHash -and
        (Canonical ([string]$reviewedReport.automated_report)) -eq (Canonical $checkReport.FullName) -and
        $reviewedReport.automated_report_sha256 -eq (Sha256 $checkReport.FullName) -and
        (Canonical ([string]$reviewedReport.candidate_app)) -eq $candidateApp -and
        $reviewedReport.data_path -eq $data -and $reviewedReport.cache_path -eq $cache -and $reviewedReport.current_app -eq $expectedCurrent
    if (-not $sameIdentity) { throw 'Candidate or fixture identity differs from the reviewed report; activation was refused.' }
    $report = [ordered]@{}
    foreach ($property in $reviewedReport.PSObject.Properties) { $report[$property.Name] = $property.Value }
    $report.status = 'activating'
} else {
    if (Test-Path -LiteralPath $reportPath) { throw "Candidate report already exists and cannot be replaced: $reportPath" }
    $report = [ordered]@{
        schema = 1; created_at = [DateTime]::UtcNow.ToString('o'); status = if ($Activate) { 'activating' } else { 'built_pending_activation' }
        profile = 'test'; external_requests = 0; commit = $Commit; build_id = $manifest.build_id; dirty = $false
        source_sha256 = $manifest.source_sha256; manifest_sha256 = Sha256 $manifestPath
        version = $manifest.version; portable_zip = $portablePath; portable_sha256 = $portableHash
        portable_sidecar = $portableSidecar; portable_sidecar_sha256 = Sha256 $portableSidecar
        fixture_version = $fixtureVersion; fixture_manifest_sha256 = $fixtureHash
        automated_report = $checkReport.FullName; automated_report_sha256 = Sha256 $checkReport.FullName
        candidate_app = $candidateApp; data_path = $data; cache_path = $cache; current_app = $expectedCurrent
    }
    Save-CandidateReport $report $reportPath
}

if ($Activate) {
    $lockPath = Join-Path $runCenterRoot 'maintenance.lock'
    try { $maintenanceLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another Run Center start or TEST maintenance operation is active. TEST was not replaced.' }
    $report.status = 'activation_preparing'
    Save-CandidateReport $report $reportPath
    $runner = Join-Path $runCenterRoot 'run-center.ps1'
    $listener = Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -Profile TEST -Action Stop -ConfigPath $ConfigPath -NoOpen
        if ($LASTEXITCODE -ne 0) { throw 'Run Center could not stop its registered TEST instance. Current program was left unchanged.' }
    }
    if (Get-NetTCPConnection -LocalPort ([int]$config.profiles.TEST.port) -State Listen -ErrorAction SilentlyContinue) {
        throw 'TEST port remains occupied; no app or data replacement was made.'
    }

    $runId = "$($manifest.build_id)-$($Commit.Substring(0,12))"
    $stageRoot = Join-Path 'K:\GeWuTest\Staging' $runId
    $stageApp = Join-Path $stageRoot 'app'
    $backupRoot = Join-Path 'K:\GeWuTest\Backups' $runId
    if ((Test-Path -LiteralPath $stageRoot) -or (Test-Path -LiteralPath $backupRoot)) { throw 'Candidate staging or backup path already exists; refusing to overwrite it.' }
    New-Item -ItemType Directory -Force -Path $stageRoot, $backupRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $stageApp | Out-Null
    Expand-Archive -LiteralPath $portablePath -DestinationPath $stageApp
    Test-ManifestFiles $stageApp $manifest
    $dataBackup = Copy-SanitizedBackup $data (Join-Path $backupRoot 'Data')
    $cacheBackup = Copy-SanitizedBackup $cache (Join-Path $backupRoot 'Cache')
    $report.data_backup = $dataBackup
    $report.cache_backup = $cacheBackup
    $report.backup_policy = 'credentials and logs excluded; original TEST data remains untouched'

    $currentApp = Join-Path $CurrentRoot 'app'
    $previousManifestPath = Join-Path $currentApp 'build-manifest.json'
    if (Test-Path -LiteralPath $currentApp) {
        if (-not (Test-Path -LiteralPath $previousManifestPath)) { throw 'Current TEST program has no build manifest; it was not replaced.' }
        $previousManifest = Get-Content -LiteralPath $previousManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Test-ManifestFiles $currentApp $previousManifest
        $report.previous_build_id = $previousManifest.build_id
        $report.previous_commit = $previousManifest.commit
        $report.previous_build_manifest_sha256 = Sha256 $previousManifestPath
    }
    $previousApp = Join-Path 'K:\GeWuTest\Rollback\Previous' 'app'
    $history = Join-Path 'K:\GeWuTest\Rollback\History' (Get-Date -Format 'yyyyMMdd-HHmmss')
    if (-not (Test-Path -LiteralPath $CurrentRoot -PathType Container)) { throw "TEST Current root is missing: $CurrentRoot" }
    if (Test-Path -LiteralPath $previousApp) {
        New-Item -ItemType Directory -Force -Path $history | Out-Null
        Move-Item -LiteralPath $previousApp -Destination (Join-Path $history 'app')
    }
    if (-not (Test-Path -LiteralPath $previousApp)) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $previousApp) | Out-Null }
    $oldMoved = $false
    try {
        if (Test-Path -LiteralPath $currentApp) { Move-Item -LiteralPath $currentApp -Destination $previousApp; $oldMoved = $true }
        Move-Item -LiteralPath $stageApp -Destination $currentApp
    } catch {
        if (-not (Test-Path -LiteralPath $currentApp) -and $oldMoved -and (Test-Path -LiteralPath $previousApp)) {
            Move-Item -LiteralPath $previousApp -Destination $currentApp
        }
        $report.status = 'activation_failed_program_swap'
        $report.activation_error = $_.Exception.Message
        Save-CandidateReport $report $reportPath
        throw
    }
    $report.status = 'activated_pending_acceptance'
    $report.previous_app = if ($oldMoved) { $previousApp } else { $null }
    $report.build_manifest_sha256 = Sha256 (Join-Path $currentApp 'build-manifest.json')
    Save-CandidateReport $report $reportPath
    if ($StartAfter) {
        $maintenanceLock.Dispose(); $maintenanceLock = $null
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -Profile TEST -Action Start -ExternalEffectsMode offline -ConfigPath $ConfigPath -NoOpen
        if ($LASTEXITCODE -ne 0) {
            $report.status = 'start_failed_rollback_available'
            $report.start_failure = 'Run Center did not confirm the candidate as an offline TEST runtime.'
            Save-CandidateReport $report $reportPath
            throw 'TEST candidate failed startup identity checks; activation report and rollback material were preserved.'
        } else {
            try { $status = Invoke-RestMethod -Uri "$($config.profiles.TEST.url)/api/runtime/status" -TimeoutSec 5 }
            catch {
                $report.status = 'start_failed_rollback_available'
                $report.start_failure = 'Runtime status endpoint did not respond after candidate start.'
                Save-CandidateReport $report $reportPath
                throw 'TEST candidate did not return runtime status; activation report and rollback material were preserved.'
            }
            if ($status.profile -ne 'test' -or $status.instance_id -ne 'TEST' -or $status.source.commit -ne $Commit -or $status.external_effects.policy -ne 'offline') {
                $report.status = 'start_failed_rollback_available'
                $report.start_failure = 'Runtime status did not match the clean offline TEST candidate.'
                Save-CandidateReport $report $reportPath
                throw 'Started TEST identity does not match the candidate manifest.'
            }
            $report.status = 'running_pending_manual_acceptance'
            Save-CandidateReport $report $reportPath
        }
    }
}

$reportHash = Sha256 $reportPath
Write-Output "TEST candidate: $($manifest.build_id)"
Write-Output "Commit: $Commit; automated report SHA-256: $($report.automated_report_sha256)"
Write-Output "Candidate report: $reportPath (SHA-256 $reportHash)"
if ($Activate) { Write-Output "TEST program updated; data backup: $($report.data_backup.path); status=$($report.status)" }
