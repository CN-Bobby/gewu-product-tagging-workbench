param(
    [string]$Storage = '',
    [int]$Port = 0,
    [switch]$NoLaunch,
    [switch]$NoCurrent
)
$ErrorActionPreference = 'Stop'

# Legacy-compatible candidate builder. It builds a clean dev candidate only; it does not change
# the fixed K:\GeWuTest current pointer or start an unpromoted build.

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing the development Python environment: $python. Install dependencies per docs/WINDOWS_BUILD.md first."
}
$version = (Get-Content -LiteralPath (Join-Path $projectRoot 'VERSION.json') -Raw | ConvertFrom-Json).version
$commit = (& git -C $projectRoot rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'git rev-parse failed; is this a Git worktree?' }
Write-Output "Building development test version: $version+$commit"

Push-Location $projectRoot
try {
    $raw = & $python (Join-Path $PSScriptRoot 'build_windows.py') --dev
    if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
$json = $raw | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
if (-not $json) { throw 'The build produced no parseable result line.' }
$result = $json | ConvertFrom-Json

Write-Output ''
Write-Output "build     : $($result.directory)"
Write-Output "app       : $($result.app)"
Write-Output "build_id  : $($result.build_id)"
Write-Output "commit    : $($result.commit)  branch=$($result.branch)  mode=$($result.build_mode)"
Write-Output "dirty     : $($result.dirty)"
Write-Output "storage   : $($result.storage_mode)"

Write-Output ''
Write-Output 'Candidate only: the fixed K:\GeWuTest\Current was not changed.'
Write-Output 'Review the manifest and follow docs\WINDOWS_BUILD.md before promoting it.'
