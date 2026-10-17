[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Checklist,
    [ValidateSet('passed', 'failed')][string]$Status = 'passed',
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$Url = 'http://127.0.0.1:8895'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or -not (Test-Path -LiteralPath $Checklist -PathType Leaf)) {
    throw 'Run Center config or checklist is missing.'
}
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reports = [IO.Path]::GetFullPath($config.profiles.TEST.reports).TrimEnd('\')
$path = [IO.Path]::GetFullPath($Checklist)
if (-not $path.StartsWith($reports + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Checklist is outside K:\GeWuTest\Reports.' }
$text = Get-Content -LiteralPath $path -Raw -Encoding UTF8
$build = [regex]::Match($text, '(?m)^- Build ID: (.+)$').Groups[1].Value.Trim()
$version = [regex]::Match($text, '(?m)^- Version: (.+)$').Groups[1].Value.Trim()
$commit = [regex]::Match($text, '(?m)^- Commit: ([0-9a-f]{40})$').Groups[1].Value.Trim()
$candidateReportPath = [regex]::Match($text, '(?m)^- Candidate report: (.+)$').Groups[1].Value.Trim()
$candidateReportHash = [regex]::Match($text, '(?m)^- Candidate report SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$appManifestHash = [regex]::Match($text, '(?m)^- App manifest SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$portableZip = [regex]::Match($text, '(?m)^- Portable ZIP: (.+)$').Groups[1].Value.Trim()
$portableHash = [regex]::Match($text, '(?m)^- Portable ZIP SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$portableSidecar = [regex]::Match($text, '(?m)^- Portable sidecar: (.+)$').Groups[1].Value.Trim()
$portableSidecarHash = [regex]::Match($text, '(?m)^- Portable sidecar SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$policy = [regex]::Match($text, '(?m)^- TEST policy: (offline|live_validation)$').Groups[1].Value.Trim()
$fixture = [regex]::Match($text, '(?m)^- Fixture: (.+)$').Groups[1].Value.Trim()
$fixtureHash = [regex]::Match($text, '(?m)^- Fixture manifest SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$automatedPath = [regex]::Match($text, '(?m)^- Automated report: (.+)$').Groups[1].Value.Trim()
$automatedHash = [regex]::Match($text, '(?m)^- Automated report SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$items = @([regex]::Matches($text, '(?m)^- \[(x| )\] (.+)$') | ForEach-Object {
    [pscustomobject]@{ passed = $_.Groups[1].Value -eq 'x'; label = $_.Groups[2].Value.Trim() }
})
if (-not $build -or -not $version -or -not $commit -or -not $candidateReportPath -or -not $candidateReportHash -or
    -not $appManifestHash -or -not $portableZip -or -not $portableHash -or -not $portableSidecar -or -not $portableSidecarHash -or
    -not $policy -or -not $fixture -or -not $fixtureHash -or -not $automatedPath -or -not $automatedHash -or $items.Count -eq 0) {
    throw 'Checklist metadata is incomplete or malformed.'
}
if ($Status -eq 'passed' -and @($items | Where-Object { -not $_.passed }).Count -gt 0) {
    throw 'A passing manual report requires every checklist item to be checked.'
}
if (-not (Test-Path -LiteralPath $automatedPath -PathType Leaf)) { throw 'Bound automated report is missing.' }
$actualAutomatedHash = (Get-FileHash -LiteralPath $automatedPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualAutomatedHash -ne $automatedHash) { throw 'Bound automated report hash changed.' }
$candidateReportPath = [IO.Path]::GetFullPath($candidateReportPath)
$portableZip = [IO.Path]::GetFullPath($portableZip)
$portableSidecar = [IO.Path]::GetFullPath($portableSidecar)
foreach ($boundFile in @($candidateReportPath, $portableZip, $portableSidecar)) {
    if (-not (Test-Path -LiteralPath $boundFile -PathType Leaf)) { throw "Bound candidate evidence is missing: $boundFile" }
}
if ((Get-FileHash -LiteralPath $candidateReportPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $candidateReportHash -or
    (Get-FileHash -LiteralPath $portableZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $portableHash -or
    (Get-FileHash -LiteralPath $portableSidecar -Algorithm SHA256).Hash.ToLowerInvariant() -ne $portableSidecarHash) {
    throw 'Candidate report, portable ZIP or checksum sidecar changed after the checklist was created.'
}
$candidateReport = Get-Content -LiteralPath $candidateReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($candidateReport.status -ne 'running_pending_manual_acceptance' -or $candidateReport.version -ne $version -or
    $candidateReport.build_id -ne $build -or $candidateReport.commit -ne $commit -or
    [IO.Path]::GetFullPath([string]$candidateReport.portable_zip) -ine $portableZip -or
    [string]$candidateReport.portable_sha256 -ne $portableHash -or
    [string]$candidateReport.portable_sidecar_sha256 -ne $portableSidecarHash -or
    [string]$candidateReport.manifest_sha256 -ne $appManifestHash) {
    throw 'Candidate report identity does not match the manual checklist.'
}
$automated = Get-Content -LiteralPath $automatedPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($automated.status -ne 'passed' -or $automated.profile -ne 'test' -or
    $automated.commit -ne $commit -or $automated.dirty -ne $false -or $automated.external_requests -ne 0 -or
    $automated.report_scope -notlike '*offline*') {
    throw 'The bound automated report must be a passing, clean, same-commit offline report with zero external requests.'
}
$fixturePath = 'K:\GeWuFixtures\project-library-v1\fixture-manifest.json'
if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $fixtureHash) {
    throw 'Fixed acceptance fixture changed after the checklist was created.'
}
$runtime = Invoke-RestMethod -Uri "$Url/api/runtime/status" -TimeoutSec 5
if ($runtime.profile -ne 'test' -or $runtime.external_effects.policy -ne $policy -or
    $runtime.source.build_id -ne $build -or $runtime.source.commit -ne $commit -or $runtime.source.dirty -ne $false) {
    throw 'The running TEST build changed since manual acceptance began.'
}
$runningManifestPath = Join-Path ([IO.Path]::GetFullPath([string]$config.profiles.TEST.app)) 'build-manifest.json'
if (-not (Test-Path -LiteralPath $runningManifestPath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $runningManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $appManifestHash) {
    throw 'Running TEST app manifest changed after the checklist was created.'
}
$expectedData = [IO.Path]::GetFullPath([string]$config.profiles.TEST.data).TrimEnd('\')
$actualData = [IO.Path]::GetFullPath([string]$runtime.data.path).TrimEnd('\')
if ($expectedData -ine [IO.Path]::GetFullPath('K:\GeWuTest\Data').TrimEnd('\') -or $actualData -ine $expectedData) {
    throw "The running TEST instance is not using the isolated TEST data directory: $actualData"
}
$expectedInstanceId = if ($policy -eq 'live_validation') { 'TEST-LIVE-VALIDATION' } else { 'TEST' }
if ($runtime.instance_id -ne $expectedInstanceId) { throw "The running TEST instance id does not match the recorded policy $policy." }
$liveCallRecord = $null
$externalRequests = 0
$externalRequestMeasurement = 'offline report; zero external requests'
if ($policy -eq 'live_validation') {
    function Get-ChecklistField([string]$Label) {
        $match = [regex]::Match($text, '(?m)^- ' + [regex]::Escape($Label) + ':\s*(.+)$')
        if (-not $match.Success) { return '' }
        return $match.Groups[1].Value.Trim()
    }
    $liveCallRecord = [ordered]@{
        service = (Get-ChecklistField 'Live-call service')
        workflow_run_id = (Get-ChecklistField 'Live-call workflow/run ID')
        selected_input_scope = (Get-ChecklistField 'Live-call selected input scope')
        authorized_ceiling = (Get-ChecklistField 'Live-call authorized ceiling')
        observed_calls_user_entered = (Get-ChecklistField 'Live-call observed calls (user-entered, not machine-measured)')
        result_path_user_entered = (Get-ChecklistField 'Live-call result path (user-entered)')
    }
    $unfinished = @($liveCallRecord.GetEnumerator() | Where-Object {
        -not $_.Value -or $_.Value -match '^\[.*\]$|^fill\b|^tbd\b|^pending\b'
    })
    if ($unfinished.Count -gt 0) {
        throw 'Live validation requires a user-entered live-call record: service, workflow/run ID, selected input scope, authorized ceiling, observed calls and result path.'
    }
    $externalRequests = $null
    $externalRequestMeasurement = 'not machine-measured; see user-entered live-call record'
}
$checklistHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
$record = [ordered]@{
    schema = 2; run_id = [Guid]::NewGuid().ToString('N'); created_at = [DateTime]::UtcNow.ToString('o')
    status = $Status; profile = 'test'; test_policy = $policy; external_requests = $externalRequests
    external_request_measurement = $externalRequestMeasurement; live_call_record = $liveCallRecord
    version = $version; build_id = $build; commit = $commit; dirty = $false
    candidate_app = [IO.Path]::GetFullPath([string]$config.profiles.TEST.app)
    candidate_report = $candidateReportPath; candidate_report_sha256 = $candidateReportHash
    app_manifest_sha256 = $appManifestHash; portable_zip = $portableZip; portable_sha256 = $portableHash
    portable_sidecar = $portableSidecar; portable_sidecar_sha256 = $portableSidecarHash
    data_path = $runtime.data.path; fixture_version = $fixture; fixture_manifest_sha256 = $fixtureHash
    automated_report = $automatedPath; automated_report_sha256 = $automatedHash
    manual_checklist = $path; manual_checklist_sha256 = $checklistHash; checklist_items = $items
}
$recordPath = Join-Path $reports ("manual-result-$build-$($record.run_id).json")
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding UTF8
$recordHash = (Get-FileHash -LiteralPath $recordPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "Manual acceptance recorded: $Status"
Write-Output "Record: $recordPath (SHA-256 $recordHash)"
