[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CandidateReport,
    [Parameter(Mandatory = $true)][string]$Checklist,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json')
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$rcRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
$expectedConfig = Join-Path $rcRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfig) { throw "Run Center config is pinned to $expectedConfig." }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reportsRoot = 'K:\GeWuDev\Reports'
$expectedData = 'K:\GeWuDev\Data'
$expectedCache = 'K:\GeWuDev\Cache'
foreach ($pair in @(@($config.profiles.DEV.reports,$reportsRoot),@($config.profiles.DEV.data,$expectedData),@($config.profiles.DEV.cache,$expectedCache))) {
    if ([IO.Path]::GetFullPath([string]$pair[0]).TrimEnd('\') -ine $pair[1]) { throw 'DEV reports/data/cache differs from the fixed isolated DEV paths.' }
}
Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
$bundle = Test-DgCandidateBundle $CandidateReport $reportsRoot (Join-Path $sourceRoot 'builds')
$checklistPath = Get-DgCanonicalExistingPath $Checklist 'Human DEV checklist'
$null = Assert-DgUnderRoot $checklistPath (Join-Path $reportsRoot 'manual-dev') 'Human DEV checklist'
Assert-DgPlainPath $checklistPath 'Human DEV checklist' | Out-Null
$checklistText = Get-Content -LiteralPath $checklistPath -Raw -Encoding UTF8
$requiredItems = @(
    'The running window and API identify profile=dev, instance_id=DEV, policy=offline, and this exact build ID/commit/source digest.',
    'Existing archive/research records open and can be read, edited, and saved without invoking external services.',
    'Saved archive edits, competition-table content, evidence references, and navigation persist after refresh/reopen.',
    'No TEST endpoint or TEST data path was used during this acceptance.',
    'I personally performed these checks; this file does not represent automated test output.'
)
$passedItems = [System.Collections.Generic.List[object]]::new()
foreach ($label in $requiredItems) {
    $escaped = [Regex]::Escape($label)
    if ($checklistText -notmatch "(?m)^- \[x\] $escaped\s*$") { throw "Human checklist item is not explicitly checked: $label" }
    $passedItems.Add([pscustomobject]@{ label = $label; passed = $true })
}
if ($checklistText -match '(?m)^- \[ \] ') { throw 'Human checklist still contains unchecked items.' }
if ($checklistText -match '(?im)^\s*(FAIL|FAILED|BLOCKED)\s*:') { throw 'Human checklist records a failure or blocker.' }
if ($checklistText -notmatch '(?m)^- Run ID: ([0-9a-f]{32})$') { throw 'Checklist Run ID is missing or invalid.' }
$manualRunId = $Matches[1]
if ($checklistText -notmatch '(?m)^- Status: awaiting human acceptance$') { throw 'Checklist status marker was edited; keep it as awaiting human acceptance.' }
foreach ($match in @(
    @{ pattern = '(?m)^- Version: (\S+)$'; expected = $bundle.version },
    @{ pattern = '(?m)^- Build ID: (\S+)$'; expected = $bundle.build_id },
    @{ pattern = '(?m)^- Commit: ([0-9a-f]{40})$'; expected = $bundle.commit },
    @{ pattern = '(?m)^- Source SHA-256: ([0-9a-f]{64})$'; expected = $bundle.source_sha256 },
    @{ pattern = '(?m)^- Candidate report SHA-256: ([0-9a-f]{64})$'; expected = $bundle.report_sha256 },
    @{ pattern = '(?m)^- Portable ZIP SHA-256: ([0-9a-f]{64})$'; expected = $bundle.portable_sha256 },
    @{ pattern = '(?m)^- Offline automated report SHA-256: ([0-9a-f]{64})$'; expected = $bundle.automated_report_sha256 }
)) {
    if ($checklistText -notmatch $match.pattern -or $Matches[1] -ine $match.expected) { throw 'Checklist build identity or artifact hash differs from the immutable candidate.' }
}

$runRecordPath = Join-Path $rcRoot 'runtime\DEV.json'
$runRecord = Read-DgJson $runRecordPath 'Run Center DEV record'
if ($runRecord.profile -ne 'DEV' -or [IO.Path]::GetFullPath([string]$runRecord.dev_candidate_report) -ine $bundle.report_path -or
    [string]$runRecord.dev_candidate_report_sha256 -ne $bundle.report_sha256 -or
    [IO.Path]::GetFullPath([string]$runRecord.candidate_app) -ine $bundle.candidate_app -or
    $runRecord.build_id -ne $bundle.build_id -or $runRecord.commit -ne $bundle.commit -or
    [IO.Path]::GetFullPath([string]$runRecord.data) -ine $expectedData -or
    [IO.Path]::GetFullPath([string]$runRecord.cache) -ine $expectedCache) {
    throw 'Run Center is not recording this exact frozen candidate with isolated DEV data/cache.'
}
$apiUrl = "http://127.0.0.1:$([int]$config.profiles.DEV.apiPort)"
try {
    $health = Invoke-RestMethod -Uri "$apiUrl/api/health" -TimeoutSec 3
    $runtime = Invoke-RestMethod -Uri "$apiUrl/api/runtime/status" -TimeoutSec 3
} catch { throw "Live DEV candidate identity is unavailable: $($_.Exception.Message)" }
if ($health.application -ne 'product-tagging-workbench' -or $health.status -ne 'ok' -or
    $health.version -ne $bundle.version -or $health.build_id -ne $bundle.build_id -or $health.commit -ne $bundle.commit -or
    $health.source_sha256 -ne $bundle.source_sha256 -or (Get-DgCanonicalPath $health.storage 'DEV health storage') -ine $expectedData -or
    $runtime.profile -ne 'dev' -or $runtime.instance_id -ne 'DEV' -or $runtime.external_effects.policy -ne 'offline' -or
    $runtime.source.build_id -ne $bundle.build_id -or $runtime.source.commit -ne $bundle.commit -or
    $runtime.source.source_sha256 -ne $bundle.source_sha256 -or (Get-DgCanonicalPath $runtime.data.path 'DEV runtime data') -ine $expectedData) {
    throw 'Live runtime does not match this offline DEV candidate identity. No passing result was created.'
}
try { $listenerPid = [int](Get-NetTCPConnection -LocalPort ([int]$config.profiles.DEV.apiPort) -State Listen -ErrorAction Stop | Select-Object -First 1 -ExpandProperty OwningProcess) }
catch { throw 'Cannot verify the listener PID for the live DEV candidate.' }
$runtimeRecord = @($runRecord.processes | Where-Object role -eq 'app-runtime') | Select-Object -First 1
if (-not $runtimeRecord -or [int]$runtimeRecord.pid -ne $listenerPid) { throw 'Live DEV listener PID is not the Run Center recorded candidate runtime process.' }
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
if (-not $process -or [IO.Path]::GetFullPath([string]$process.ExecutablePath) -ine [IO.Path]::GetFullPath([string]$runtimeRecord.executable) -or
    [string]$process.CommandLine -notlike "*$($runtimeRecord.identity_token)*") {
    throw 'Live DEV listener process no longer matches its Run Center executable and command identity.'
}
$recordSnapshotPath = Join-Path (Split-Path -Parent $checklistPath) "run-center-DEV-$manualRunId.json"
if (Test-Path -LiteralPath $recordSnapshotPath) { throw "Run Center evidence snapshot already exists: $recordSnapshotPath" }
$recordBytes = [IO.File]::ReadAllBytes($runRecordPath)
$snapshotStream = [IO.File]::Open($recordSnapshotPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $snapshotStream.Write($recordBytes, 0, $recordBytes.Length); $snapshotStream.Flush($true) }
finally { $snapshotStream.Dispose() }

$resultPath = Join-Path (Split-Path -Parent $checklistPath) "result-dev-$($bundle.version)-$($bundle.commit.Substring(0,12))-$manualRunId.json"
if (Test-Path -LiteralPath $resultPath) { throw "Manual result already exists and cannot be overwritten: $resultPath" }
$result = [ordered]@{
    schema = 1; status = 'passed'; profile = 'dev'; runtime_policy = 'offline'; runtime_instance_id = 'DEV'
    run_id = $manualRunId; human_tested_at = [DateTime]::UtcNow.ToString('o'); version = $bundle.version; build_id = $bundle.build_id
    commit = $bundle.commit; dirty = $false; source_sha256 = $bundle.source_sha256; data_path = $expectedData
    candidate_report = $bundle.report_path; candidate_report_sha256 = $bundle.report_sha256
    candidate_app = $bundle.candidate_app; app_manifest_sha256 = $bundle.manifest_sha256
    portable_zip = $bundle.portable_zip; portable_sha256 = $bundle.portable_sha256
    portable_sidecar = $bundle.portable_sidecar; portable_sidecar_sha256 = $bundle.portable_sidecar_sha256
    automated_report = $bundle.automated_report; automated_report_sha256 = $bundle.automated_report_sha256
    manual_checklist = $checklistPath; manual_checklist_sha256 = Get-DgSha256 $checklistPath
    checklist_items = @($passedItems.ToArray()); run_center_record = $recordSnapshotPath
    run_center_record_sha256 = Get-DgSha256 $recordSnapshotPath
}
$temp = "$resultPath.tmp"
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding UTF8
try {
    Test-DgManualAcceptance $temp $reportsRoot (Join-Path $sourceRoot 'builds') $expectedData | Out-Null
    Move-Item -LiteralPath $temp -Destination $resultPath
} finally {
    if (Test-Path -LiteralPath $temp -PathType Leaf) { Remove-Item -LiteralPath $temp -Force }
}
Write-Output "Human-confirmed DEV acceptance result: $resultPath"
Write-Output "Run ID: $manualRunId; build: $($bundle.build_id); candidate: $($bundle.report_path)"
