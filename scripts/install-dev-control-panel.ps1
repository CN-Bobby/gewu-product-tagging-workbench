[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$PublishDirectory = '',
    [string]$DesktopPath = [Environment]::GetFolderPath('Desktop')
)
$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
if (-not $PublishDirectory) { $PublishDirectory = Join-Path $ProjectRoot 'tools\DevControlPanel\publish' }

function Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
$ProjectRoot = Canonical $ProjectRoot
$PublishDirectory = Canonical $PublishDirectory
$runCenterRoot = Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'
$configPath = Join-Path $runCenterRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "Run Center 未安装：$configPath" }
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ((Canonical ([string]$config.projectRoot)) -ine $ProjectRoot) {
    throw "Run Center 的 DEV 源码目录与本次构建不一致：$($config.projectRoot)"
}
$exeSource = Join-Path $PublishDirectory 'GeWuDevControl.exe'
if (-not (Test-Path -LiteralPath $exeSource -PathType Leaf)) { throw "请先发布开发控制台：$exeSource" }
$runnerSource = Join-Path $ProjectRoot 'scripts\run-center.ps1'
$statusSource = Join-Path $ProjectRoot 'scripts\run-center-status.psm1'
foreach ($source in @($runnerSource, $statusSource)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "缺少运行中心文件：$source" }
}
$installRoot = Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\DevControlPanel'
$buildHash = (Get-FileHash -LiteralPath $exeSource -Algorithm SHA256).Hash.Substring(0, 12).ToLowerInvariant()
$versionDir = Join-Path $installRoot "versions\$buildHash"
$exeTarget = Join-Path $versionDir 'GeWuDevControl.exe'
$shortcutPath = Join-Path $DesktopPath '格物 · 开发控制台.lnk'
$shell = New-Object -ComObject WScript.Shell

# Inspect shortcut ownership before changing any installed file.
if (Test-Path -LiteralPath $shortcutPath) {
    $existing = $shell.CreateShortcut($shortcutPath)
    if (-not $existing.TargetPath -or
        -not (Canonical $existing.TargetPath).StartsWith((Canonical $installRoot) + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "桌面同名入口并非本控制台创建，未覆盖：$shortcutPath"
    }
}

New-Item -ItemType Directory -Path $versionDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $exeTarget -PathType Leaf)) {
    Get-ChildItem -LiteralPath $PublishDirectory -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $versionDir $_.Name) -ErrorAction Stop
    }
}
if ((Get-FileHash -LiteralPath $exeTarget -Algorithm SHA256).Hash -ne
    (Get-FileHash -LiteralPath $exeSource -Algorithm SHA256).Hash) {
    throw "安装副本与构建文件哈希不一致：$exeTarget"
}

$backupDir = Join-Path $installRoot 'backups'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$replacements = @(
    @{ source = $runnerSource; target = (Join-Path $runCenterRoot 'run-center.ps1'); name = 'run-center.ps1' },
    @{ source = $statusSource; target = (Join-Path $runCenterRoot 'run-center-status.psm1'); name = 'run-center-status.psm1' }
)
$changed = [System.Collections.Generic.List[object]]::new()
try {
    foreach ($item in $replacements) {
        $target = [string]$item.target
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq
            (Get-FileHash -LiteralPath $item.source -Algorithm SHA256).Hash) { continue }
        $backup = Join-Path $backupDir "$($item.name).$stamp.bak"
        $temp = "$target.new.$stamp"
        Copy-Item -LiteralPath $item.source -Destination $temp -ErrorAction Stop
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            [IO.File]::Replace($temp, $target, $backup)
        } else {
            [IO.File]::Move($temp, $target)
            $backup = $null
        }
        $changed.Add([pscustomobject]@{ target = $target; backup = $backup })
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $backupDir "desktop-entry.$stamp.lnk")
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exeTarget
    $shortcut.WorkingDirectory = $versionDir
    $shortcut.IconLocation = "$exeTarget,0"
    $shortcut.Description = '格物 DEV 开发控制台；PROD 仅查看状态'
    $shortcut.Save()
} catch {
    foreach ($item in $changed) {
        if ($item.backup -and (Test-Path -LiteralPath $item.backup -PathType Leaf)) {
            Copy-Item -LiteralPath $item.backup -Destination $item.target -Force
        }
    }
    throw
}
Write-Output "开发控制台已安装：$exeTarget"
Write-Output "桌面入口：$shortcutPath"
Write-Output "Run Center 配置与原 DEV／PROD 快捷方式未修改；运行脚本备份：$backupDir"
