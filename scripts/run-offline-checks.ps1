[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ReportDir = 'K:\GeWuTest\Reports'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$ReportDir = [IO.Path]::GetFullPath($ReportDir)
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'backend\app.py'))) { throw "Not a workbench checkout: $ProjectRoot" }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.git'))) {
    $gitRoot = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($gitRoot.Trim()) -ne $ProjectRoot) { throw 'Could not identify the source Git worktree.' }
}
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Python is unavailable. Install project dependencies before running offline checks.' }
    $python = $pythonCommand.Source
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) { throw 'npm.cmd is unavailable. Install Node.js before running offline checks.' }
$npm = $npmCommand.Source
$version = (Get-Content -LiteralPath (Join-Path $ProjectRoot 'VERSION.json') -Raw | ConvertFrom-Json).version
$commit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'git rev-parse failed.' }
$short = $commit.Substring(0, 12)
$gitStatus = @(& git -C $ProjectRoot status --porcelain)
if ($LASTEXITCODE -ne 0) { throw 'git status failed.' }
$dirty = $gitStatus.Count -gt 0
$started = [DateTime]::UtcNow
$runId = [Guid]::NewGuid().ToString('N')
$sandbox = Join-Path $env:TEMP "GeWuOfflineChecks\$runId"
$env:GEWU_PROFILE = 'test'
$env:GEWU_INSTANCE_ID = 'OFFLINE-CHECKS'
$env:GEWU_ALLOW_EXTERNAL_EFFECTS = '0'
$env:TAG_STORAGE = Join-Path $sandbox 'Data'
$env:TAG_CACHE = Join-Path $sandbox 'Cache'
$env:GEWU_REPORTS_DIR = $ReportDir
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $ProjectRoot
New-Item -ItemType Directory -Force -Path $ReportDir, $sandbox | Out-Null

$steps = [System.Collections.Generic.List[object]]::new()
function Invoke-Check([string]$Name, [string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory) {
    Write-Host "[offline] $Name"
    Push-Location $WorkingDirectory
    try {
        $lines = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
    foreach ($line in $lines) { Write-Host $line }
    $summary = switch -Regex ($lines) {
        'passed[, ]+[^\r\n]+skipped' { $_; break }
        '^# (tests|pass|fail|duration_ms)\b' { $_ }
        'built in [0-9.]+s' { $_ }
    }
    $status = if ($exitCode -eq 0) { 'passed' } else { 'failed' }
    $script:steps.Add([pscustomobject]@{ name = $Name; status = $status; exit_code = $exitCode; summary = @($summary | Select-Object -Last 5) })
    return $exitCode
}

$rootCode = 0
$rootCode = [Math]::Max($rootCode, (Invoke-Check 'backend-pytest' $python @('-m','pytest','-q') $ProjectRoot))
$frontend = Join-Path $ProjectRoot 'frontend'
$rootCode = [Math]::Max($rootCode, (Invoke-Check 'frontend-tests' $npm @('test','--prefix',$frontend) $ProjectRoot))
$rootCode = [Math]::Max($rootCode, (Invoke-Check 'frontend-build' $npm @('run','build','--prefix',$frontend) $ProjectRoot))
$finished = [DateTime]::UtcNow
$report = [ordered]@{
    schema = 1
    run_id = $runId
    created_at = $finished.ToString('o')
    started_at = $started.ToString('o')
    profile = 'test'
    external_requests = 0
    network_guard = 'loopback-only socket guard'
    status = if ($rootCode -eq 0) { 'passed' } else { 'failed' }
    build_id = "$version+$short"
    commit = $commit
    dirty = $dirty
    fixture_version = 'not-used'
    report_scope = 'offline backend and frontend checks'
    steps = @($steps)
}
$reportPath = Join-Path $ReportDir "offline-$($finished.ToString('yyyyMMdd-HHmmss'))-$($short).json"
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Output "Offline report: $reportPath"
Write-Output "Status: $($report.status); build=$($report.build_id); commit=$commit; dirty=$dirty; external_requests=0"
exit $rootCode
