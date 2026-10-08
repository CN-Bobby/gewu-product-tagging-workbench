[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$Url = 'http://127.0.0.1:8895'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$reports = [IO.Path]::GetFullPath($config.profiles.TEST.reports)
if (-not $reports.StartsWith('K:\GeWuTest\Reports', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Manual evidence must stay in K:\GeWuTest\Reports: $reports"
}
New-Item -ItemType Directory -Force -Path $reports | Out-Null
$status = Invoke-RestMethod -Uri "$Url/api/runtime/status" -TimeoutSec 5
if ($status.profile -ne 'test' -or $status.external_effects.policy -ne 'offline' -or
    $status.source.dirty -ne $false -or -not $status.source.commit) {
    throw 'The current listener is not a clean, offline TEST build. No checklist was created.'
}
$fixtureManifest = 'K:\GeWuFixtures\project-library-v1\fixture-manifest.json'
if (-not (Test-Path -LiteralPath $fixtureManifest -PathType Leaf)) { throw "Fixture manifest is missing: $fixtureManifest" }
$fixture = Get-Content -LiteralPath $fixtureManifest -Raw | ConvertFrom-Json
$fixtureHash = (Get-FileHash -LiteralPath $fixtureManifest -Algorithm SHA256).Hash.ToLowerInvariant()
$automated = Get-ChildItem -LiteralPath $reports -Filter '*.json' -File -ErrorAction SilentlyContinue |
    ForEach-Object { try { [pscustomobject]@{ file = $_; data = (Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json) } } catch { $null } } |
    Where-Object { $_ -and $_.data.status -eq 'passed' -and $_.data.commit -eq $status.source.commit } |
    Sort-Object { $_.file.LastWriteTime } -Descending | Select-Object -First 1
if (-not $automated) { throw 'No passing automated report exists for the running TEST commit.' }
$automatedHash = (Get-FileHash -LiteralPath $automated.file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$runId = [Guid]::NewGuid().ToString('N')
$path = Join-Path $reports "manual-acceptance-$($status.source.build_id)-$runId.md"
$items = @(
    '从桌面“格物 · 打开验收版”进入；版本面板显示 TEST、冻结提交、数据目录 K:\GeWuTest\Data 和 offline。',
    '展开/收起全局项目树后仍停留当前页面；切换工作表、项目资料后使用浏览器返回/前进，路由与选中项目正确。',
    '打开“泳衣选品档案”原始 Excel，切换工作表并确认商品图片缩略图可打开。',
    '打开“狗狗发声玩具”项目 Data X V1，检查商品表头、数据分页和下载。',
    '打开两个样本项目的评论 Excel，检查工作表与搜索；打开泳衣与狗狗玩具 VOC HTML 预览。',
    '窄屏约 375px 检查导航、项目树和预览面板，无横向溢出或遮挡关键操作。',
    '确认 DEV/TEST 的模型与 SellerSprite 状态只显示“已配置/未配置”，页面、日志与验收报告没有密钥。',
    '在 TEST 尝试启动模型、MCP、Amazon 采集或 VOC 任务，确认收到离线阻断；仅执行本地预览。',
    '退出并重新打开 TEST，确认提交、构建、数据目录、凭证状态和离线策略保持一致。',
    '若执行 TEST 数据重置，确认目标为 K:\GeWuTest\Data，旧目录保存在 K:\GeWuTest\Backups，PROD 哨兵不变。'
)
$content = @(
    '# 格物 TEST 人工验收', '',
    "- Run ID: $runId",
    "- Build ID: $($status.source.build_id)",
    "- Commit: $($status.source.commit)",
    "- Data: $($status.data.path)",
    "- Fixture: $($fixture.fixture_version)",
    "- Fixture manifest SHA-256: $fixtureHash",
    "- Automated report: $($automated.file.FullName)",
    "- Automated report SHA-256: $automatedHash",
    '- External calls: 0 expected (DEV/TEST offline policy)', '',
    '逐项完成后把 `[ ]` 改为 `[x]`，发现问题时写明复现步骤和页面位置。', ''
)
foreach ($item in $items) { $content += "- [ ] $item"; $content += '' }
Set-Content -LiteralPath $path -Value ($content -join "`r`n") -Encoding UTF8
Write-Output "Manual checklist created: $path"
Write-Output "Build: $($status.source.build_id); fixture: $($fixture.fixture_version); automated report hash: $automatedHash"
