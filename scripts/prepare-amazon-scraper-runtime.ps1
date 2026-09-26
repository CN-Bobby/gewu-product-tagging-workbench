param(
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Bundled = Join-Path $Root "runtime\python.exe"
    $Venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $Bundled) { $Python = $Bundled }
    elseif (Test-Path $Venv) { $Python = $Venv }
    else { $Python = "python" }
}
& $Python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium 安装失败，退出码 $LASTEXITCODE" }
Write-Host "Amazon 图片采集浏览器已准备完成。工作台启动时只会检查，不会静默下载浏览器。"
