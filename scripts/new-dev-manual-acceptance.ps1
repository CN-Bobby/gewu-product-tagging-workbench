[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CandidateReport,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json')
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$rc = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
if ([IO.Path]::GetFullPath($ConfigPath) -ine (Join-Path $rc 'config.json')) { throw 'Run Center config path is pinned.' }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reports = 'K:\GeWuDev\Reports'
if ([IO.Path]::GetFullPath([string]$config.profiles.DEV.reports).TrimEnd('\') -ine $reports) { throw 'DEV Reports is not the approved isolated path.' }
Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
$bundle = Test-DgCandidateBundle $CandidateReport $reports (Join-Path $root 'builds')
$manualRoot = Join-Path $reports 'manual-dev'
New-Item -ItemType Directory -Force -Path $manualRoot | Out-Null
Assert-DgPlainPath $manualRoot 'DEV manual evidence directory' -Directory | Out-Null
$runId = [Guid]::NewGuid().ToString('N')
$path = Join-Path $manualRoot "manual-dev-$($bundle.version)-$($bundle.commit.Substring(0,12))-$runId.md"
if (Test-Path -LiteralPath $path) { throw 'Unique DEV manual checklist path unexpectedly exists.' }
$body = @"
# DEV candidate manual acceptance

- Run ID: $runId
- Status: awaiting human acceptance
- Version: $($bundle.version)
- Build ID: $($bundle.build_id)
- Commit: $($bundle.commit)
- Source SHA-256: $($bundle.source_sha256)
- Candidate report: $($bundle.report_path)
- Candidate report SHA-256: $($bundle.report_sha256)
- Portable ZIP SHA-256: $($bundle.portable_sha256)
- Offline automated report SHA-256: $($bundle.automated_report_sha256)
- DEV data path: $($config.profiles.DEV.data)
- Runtime policy required: offline

Complete each item below by editing `[ ]` to `[x]` only after personally checking it in the launched DEV candidate. Record any failure under Notes; do not mark a failed item passed.

- [ ] The running window and API identify profile=dev, instance_id=DEV, policy=offline, and this exact build ID/commit/source digest.
- [ ] Existing archive/research records open and can be read, edited, and saved without invoking external services.
- [ ] Saved archive edits, competition-table content, evidence references, and navigation persist after refresh/reopen.
- [ ] No TEST endpoint or TEST data path was used during this acceptance.
- [ ] I personally performed these checks; this file does not represent automated test output.

If this feature requires a real model/MCP/Amazon research run, record it separately as a non-gating live-service check with its own Run ID, policy, scope, and cost; it is not represented by this offline DEV acceptance.

## Notes and failures

<!-- Record observed behavior, including failures. -->

"@
$stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($body)
    $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true)
} finally { $stream.Dispose() }
Write-Output "Created unchecked human checklist: $path"
Write-Output 'No acceptance result was created. Run finalize-dev-manual-acceptance.ps1 only after editing the checklist based on actual DEV testing.'
