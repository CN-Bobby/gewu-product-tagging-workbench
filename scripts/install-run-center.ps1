[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\数据打标项目',
    [string]$CandidateProjectRoot = $ProjectRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'),
    [string]$DesktopPath = [Environment]::GetFolderPath('Desktop'),
    [string]$DevData = 'K:\GeWuDev\Data',
    [string]$DevCache = 'K:\GeWuDev\Cache',
    [string]$Reports = 'K:\GeWuTest\Reports',
    [string]$TestApp = 'K:\GeWuTest\Current\app',
    [string]$TestData = 'K:\GeWuTest\Data',
    [string]$TestCache = 'K:\GeWuTest\Cache',
    [string]$ProdApp = 'K:\Bobbytagging\Candidate-beta.8\app',
    [string]$ProdData = 'K:\Bobbytagging\Data',
    [string]$ProdCache = 'K:\Bobbytagging\Cache'
)
$ErrorActionPreference = 'Stop'
function Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
$expectedInstallRoot = Canonical (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')
if ((Canonical $InstallRoot) -ne $expectedInstallRoot) {
    throw "Run Center installation is pinned to $expectedInstallRoot so all launch and maintenance actions share one lock and identity store."
}
$fixedPaths = @{
    DevData = 'K:\GeWuDev\Data'; DevCache = 'K:\GeWuDev\Cache'; Reports = 'K:\GeWuTest\Reports'
    TestApp = 'K:\GeWuTest\Current\app'; TestData = 'K:\GeWuTest\Data'; TestCache = 'K:\GeWuTest\Cache'
    ProdApp = 'K:\Bobbytagging\Candidate-beta.8\app'; ProdData = 'K:\Bobbytagging\Data'; ProdCache = 'K:\Bobbytagging\Cache'
}
foreach ($name in $fixedPaths.Keys) {
    if ((Canonical (Get-Variable -Name $name -ValueOnly)) -ne (Canonical $fixedPaths[$name])) {
        throw "$name is pinned to $($fixedPaths[$name]) in the initial Run Center deployment."
    }
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'backend\app.py'))) { throw "Project root is not a workbench source checkout: $ProjectRoot" }
$CandidateProjectRoot = [IO.Path]::GetFullPath($CandidateProjectRoot)
if (-not (Test-Path -LiteralPath (Join-Path $CandidateProjectRoot 'backend\app.py'))) { throw "Candidate source root is not a workbench checkout: $CandidateProjectRoot" }
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$nodeExe = if ($nodeCommand) { $nodeCommand.Source } else { '' }
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$runnerSource = Join-Path $PSScriptRoot 'run-center.ps1'
$runnerDestination = Join-Path $InstallRoot 'run-center.ps1'
$runnerText = [IO.File]::ReadAllText($runnerSource, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($runnerDestination, $runnerText, [Text.UTF8Encoding]::new($true))
$config = [ordered]@{
    schema = 1
    projectRoot = $ProjectRoot
    candidateProjectRoot = $CandidateProjectRoot
    nodeExe = $nodeExe
    profiles = [ordered]@{
        DEV = [ordered]@{ url = 'http://127.0.0.1:5174'; webPort = 5174; apiPort = 8896; data = $DevData; cache = $DevCache; providerHome = (Join-Path $InstallRoot 'profiles\DEV\providers'); reports = $Reports }
        TEST = [ordered]@{ url = 'http://127.0.0.1:8895'; port = 8895; app = $TestApp; data = $TestData; cache = $TestCache; providerHome = (Join-Path $InstallRoot 'profiles\TEST\providers'); reports = $Reports }
        PROD = [ordered]@{ url = 'http://127.0.0.1:8765'; port = 8765; app = $ProdApp; data = $ProdData; cache = $ProdCache; reports = $Reports }
    }
}
$configPath = Join-Path $InstallRoot 'config.json'
if (Test-Path -LiteralPath $configPath) {
    $existingConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($existingConfig.schema -ne 1 -or -not $existingConfig.profiles) {
        throw "Existing Run Center config is not recognized and was left unchanged: $configPath"
    }
    $config = $existingConfig
    foreach ($profile in @('DEV', 'TEST')) {
        if (-not $config.profiles.$profile.providerHome) {
            $config.profiles.$profile | Add-Member -NotePropertyName providerHome -NotePropertyValue (Join-Path $InstallRoot "profiles\$profile\providers")
        }
    }
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
} else {
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
}
$runner = Join-Path $InstallRoot 'run-center.ps1'
$shell = New-Object -ComObject WScript.Shell
$powershellPath = (Get-Command powershell.exe).Source
$desiredShortcuts = @(
    @{ profile = 'DEV'; name = '格物 · 打开开发版.lnk' },
    @{ profile = 'TEST'; name = '格物 · 打开验收版.lnk' },
    @{ profile = 'PROD'; name = '格物 · 正式工作台.lnk' }
)
# Check every destination before archiving any existing links so a conflict cannot leave a
# partially migrated desktop. Only exact legacy GeWu shortcuts are moved; their files are kept.
foreach ($item in $desiredShortcuts) {
    $shortcutPath = Join-Path $DesktopPath $item.name
    if (Test-Path -LiteralPath $shortcutPath) {
        $existing = $shell.CreateShortcut($shortcutPath)
        if ($existing.TargetPath -ne $powershellPath -or
            $existing.Arguments -notlike '*run-center.ps1*' -or
            $existing.Arguments -notmatch "-Profile\s+$($item.profile)(?:\s|$)") {
            throw "Desktop shortcut already exists and does not belong to this Run Center: $shortcutPath"
        }
    }
}
$legacyShortcuts = @(
    @{ name = '格物工作台.lnk'; kind = 'prod-beta7' },
    @{ name = '格物工作台（测试版）.lnk'; kind = 'test-legacy' }
)
$legacyDir = Join-Path $InstallRoot 'LegacyShortcuts'
$legacyToArchive = [System.Collections.Generic.List[object]]::new()
foreach ($item in $legacyShortcuts) {
    $path = Join-Path $DesktopPath $item.name
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $existing = $shell.CreateShortcut($path)
    $matchesLegacy = if ($item.kind -eq 'prod-beta7') {
        try { [IO.Path]::GetFullPath($existing.TargetPath).TrimEnd('\') -ieq 'K:\Bobbytagging\app\Workbench.exe' } catch { $false }
    } else {
        $existing.TargetPath -eq $powershellPath -and $existing.Arguments.Contains('dev-launcher.ps1') -and
            $existing.Arguments.Contains('K:\GeWuTest\Current')
    }
    if (-not $matchesLegacy) { throw "Existing GeWu shortcut has an unknown target and was left unchanged: $path" }
    $legacyToArchive.Add([pscustomobject]@{ path = $path; name = $item.name })
}
foreach ($item in $legacyToArchive) {
    New-Item -ItemType Directory -Force -Path $legacyDir | Out-Null
    $archive = Join-Path $legacyDir $item.name
    if (Test-Path -LiteralPath $archive) {
        $stem = [IO.Path]::GetFileNameWithoutExtension($item.name)
        $archive = Join-Path $legacyDir ($stem + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.lnk')
    }
    Move-Item -LiteralPath $item.path -Destination $archive
    Write-Output "Preserved old desktop shortcut at $archive"
}
foreach ($item in $desiredShortcuts) {
    $profile = $item.profile
    $shortcutPath = Join-Path $DesktopPath $item.name
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powershellPath
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Profile $profile -Action Start -ConfigPath `"$configPath`""
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = "格物工作台 $profile 本地运行入口"
    $shortcut.Save()
}
Write-Output "Run Center installed at $InstallRoot"
Write-Output "Configuration: $configPath"
Write-Output "Created desktop entries for DEV, TEST, and PROD. No service was started."
