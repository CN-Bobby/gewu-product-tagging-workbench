param(
    [string]$Storage = '',
    [int]$Port = 0,
    [switch]$NoLaunch,
    [switch]$NoCurrent
)
$ErrorActionPreference = 'Stop'

# One command for the development loop: version + short commit -> versioned build directory ->
# current pointer -> start. It never writes to releases/ and never builds an installer.

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
    $raw = & $python (Join-Path $PSScriptRoot 'build_windows.py') --dev --allow-dirty
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

if (-not $NoCurrent) {
    $pointer = Join-Path $projectRoot 'builds\current.json'
    $record = [ordered]@{
        current = $result.directory
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $pointer -Encoding UTF8
    Write-Output "current   : $($result.directory)  ->  $pointer"
}

if ($NoLaunch) { exit 0 }

$launch = @{}
if ($Storage) { $launch['Storage'] = $Storage }
if ($Port -gt 0) { $launch['Port'] = $Port }
& (Join-Path $PSScriptRoot 'dev-launcher.ps1') @launch
