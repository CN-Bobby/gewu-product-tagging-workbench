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
$commit = [regex]::Match($text, '(?m)^- Commit: ([0-9a-f]{40})$').Groups[1].Value.Trim()
$fixture = [regex]::Match($text, '(?m)^- Fixture: (.+)$').Groups[1].Value.Trim()
$fixtureHash = [regex]::Match($text, '(?m)^- Fixture manifest SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$automatedPath = [regex]::Match($text, '(?m)^- Automated report: (.+)$').Groups[1].Value.Trim()
$automatedHash = [regex]::Match($text, '(?m)^- Automated report SHA-256: ([0-9a-f]{64})$').Groups[1].Value.Trim()
$items = @([regex]::Matches($text, '(?m)^- \[(x| )\] (.+)$') | ForEach-Object {
    [pscustomobject]@{ passed = $_.Groups[1].Value -eq 'x'; label = $_.Groups[2].Value.Trim() }
})
if (-not $build -or -not $commit -or -not $fixture -or -not $fixtureHash -or -not $automatedPath -or -not $automatedHash -or $items.Count -eq 0) {
    throw 'Checklist metadata is incomplete or malformed.'
}
if ($Status -eq 'passed' -and @($items | Where-Object { -not $_.passed }).Count -gt 0) {
    throw 'A passing manual report requires every checklist item to be checked.'
}
if (-not (Test-Path -LiteralPath $automatedPath -PathType Leaf)) { throw 'Bound automated report is missing.' }
$actualAutomatedHash = (Get-FileHash -LiteralPath $automatedPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualAutomatedHash -ne $automatedHash) { throw 'Bound automated report hash changed.' }
$fixturePath = 'K:\GeWuFixtures\project-library-v1\fixture-manifest.json'
if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $fixtureHash) {
    throw 'Fixed acceptance fixture changed after the checklist was created.'
}
$runtime = Invoke-RestMethod -Uri "$Url/api/runtime/status" -TimeoutSec 5
if ($runtime.profile -ne 'test' -or $runtime.external_effects.policy -ne 'offline' -or
    $runtime.source.build_id -ne $build -or $runtime.source.commit -ne $commit -or $runtime.source.dirty -ne $false) {
    throw 'The running TEST build changed since manual acceptance began.'
}
$checklistHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
$record = [ordered]@{
    schema = 1; run_id = [Guid]::NewGuid().ToString('N'); created_at = [DateTime]::UtcNow.ToString('o')
    status = $Status; profile = 'test'; external_requests = 0; build_id = $build; commit = $commit; dirty = $false
    data_path = $runtime.data.path; fixture_version = $fixture; fixture_manifest_sha256 = $fixtureHash
    automated_report = $automatedPath; automated_report_sha256 = $automatedHash
    manual_checklist = $path; manual_checklist_sha256 = $checklistHash; checklist_items = $items
}
$recordPath = Join-Path $reports ("manual-result-$build-$($record.run_id).json")
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding UTF8
$recordHash = (Get-FileHash -LiteralPath $recordPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "Manual acceptance recorded: $Status"
Write-Output "Record: $recordPath (SHA-256 $recordHash)"
