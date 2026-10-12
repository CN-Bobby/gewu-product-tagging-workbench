[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$Url = 'http://127.0.0.1:8895'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reports = [IO.Path]::GetFullPath($config.profiles.TEST.reports)
if (-not $reports.StartsWith('K:\GeWuTest\Reports', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Manual evidence must stay in K:\GeWuTest\Reports: $reports"
}
New-Item -ItemType Directory -Force -Path $reports | Out-Null
$status = Invoke-RestMethod -Uri "$Url/api/runtime/status" -TimeoutSec 5
if ($status.profile -ne 'test' -or $status.external_effects.policy -notin @('offline', 'live_validation') -or
    $status.source.dirty -ne $false -or -not $status.source.commit) {
    throw 'The current listener is not a clean TEST build with a supported runtime policy. No checklist was created.'
}
$expectedData = [IO.Path]::GetFullPath([string]$config.profiles.TEST.data).TrimEnd('\')
$actualData = [IO.Path]::GetFullPath([string]$status.data.path).TrimEnd('\')
if ($expectedData -ine [IO.Path]::GetFullPath('K:\GeWuTest\Data').TrimEnd('\') -or $actualData -ine $expectedData) {
    throw "The current listener does not use the isolated TEST data directory: $actualData"
}
$expectedInstanceId = if ($status.external_effects.policy -eq 'live_validation') { 'TEST-LIVE-VALIDATION' } else { 'TEST' }
if ($status.instance_id -ne $expectedInstanceId) {
    throw "The current listener instance id does not match policy $($status.external_effects.policy). No checklist was created."
}
$candidateReports = @(Get-ChildItem -LiteralPath $reports -Filter "candidate-$($status.source.build_id)-$($status.source.commit.Substring(0,12)).json" -File -ErrorAction SilentlyContinue)
if ($candidateReports.Count -ne 1) { throw 'A unique frozen candidate report is required before manual acceptance.' }
$candidateReportFile = $candidateReports[0]
$candidateReport = Get-Content -LiteralPath $candidateReportFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
if ($candidateReport.status -ne 'running_pending_manual_acceptance' -or
    $candidateReport.build_id -ne $status.source.build_id -or $candidateReport.commit -ne $status.source.commit -or
    [IO.Path]::GetFullPath([string]$candidateReport.current_app).TrimEnd('\') -ine [IO.Path]::GetFullPath([string]$config.profiles.TEST.app).TrimEnd('\')) {
    throw 'The candidate report does not bind the running TEST build to its portable acceptance package.'
}
$portableZip = [IO.Path]::GetFullPath([string]$candidateReport.portable_zip)
$portableSidecar = [IO.Path]::GetFullPath([string]$candidateReport.portable_sidecar)
if (-not (Test-Path -LiteralPath $portableZip -PathType Leaf) -or -not (Test-Path -LiteralPath $portableSidecar -PathType Leaf) -or
    (Get-FileHash -LiteralPath $portableZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$candidateReport.portable_sha256 -or
    (Get-FileHash -LiteralPath $portableSidecar -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$candidateReport.portable_sidecar_sha256) {
    throw 'Portable ZIP or checksum sidecar changed after candidate activation.'
}
$candidateApp = [IO.Path]::GetFullPath([string]$config.profiles.TEST.app)
$manifestPath = Join-Path $candidateApp 'build-manifest.json'
if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$candidateReport.manifest_sha256) {
    throw 'Running TEST app manifest does not match the frozen candidate report.'
}
$candidateReportHash = (Get-FileHash -LiteralPath $candidateReportFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$fixtureManifest = 'K:\GeWuFixtures\project-library-v1\fixture-manifest.json'
if (-not (Test-Path -LiteralPath $fixtureManifest -PathType Leaf)) { throw "Fixture manifest is missing: $fixtureManifest" }
$fixture = Get-Content -LiteralPath $fixtureManifest -Raw -Encoding UTF8 | ConvertFrom-Json
$fixtureHash = (Get-FileHash -LiteralPath $fixtureManifest -Algorithm SHA256).Hash.ToLowerInvariant()
$automated = Get-ChildItem -LiteralPath $reports -Filter 'offline-*.json' -File -ErrorAction SilentlyContinue |
    ForEach-Object { try { [pscustomobject]@{ file = $_; data = (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json) } } catch { $null } } |
    Where-Object { $_ -and $_.data.status -eq 'passed' -and $_.data.profile -eq 'test' -and
        $_.data.dirty -eq $false -and $_.data.external_requests -eq 0 -and $_.data.commit -eq $status.source.commit } |
    Sort-Object { $_.file.LastWriteTime } -Descending | Select-Object -First 1
if (-not $automated) { throw 'No passing clean offline report exists for the running TEST commit.' }
$automatedHash = (Get-FileHash -LiteralPath $automated.file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$browser = Get-ChildItem -LiteralPath $reports -Filter 'browser-project-library-*.json' -File -ErrorAction SilentlyContinue |
    ForEach-Object { try { [pscustomobject]@{ file = $_; data = (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json) } } catch { $null } } |
    Where-Object { $_ -and $_.data.status -eq 'passed' -and $_.data.profile -eq 'test' -and
        $_.data.external_requests -eq 0 -and $_.data.commit -eq $status.source.commit -and
        $_.data.build_id -eq $status.source.build_id -and $_.data.fixture_manifest_sha256 -eq $fixtureHash } |
    Sort-Object { $_.file.LastWriteTime } -Descending | Select-Object -First 1
if (-not $browser) { throw 'No passing loopback browser report exists for this TEST build and fixture.' }
$browserHash = (Get-FileHash -LiteralPath $browser.file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
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
    '若执行 TEST 数据重置，确认目标为 K:\GeWuTest\Data，旧目录保存在 K:\GeWuTest\Backups，PROD 哨兵不变。'
)
$items[0] = ('从桌面“格物 · 打开验收版”进入；版本面板显示 TEST、冻结提交、隔离数据目录 K:\GeWuTest\Data 和 ' + $status.external_effects.policy + '。')
$items = @('确认本次 TEST 程序由下方记录的便携 ZIP 解压得到；包内版本、提交和清单与验收记录一致。') + $items
if ($status.external_effects.policy -eq 'offline') {
    $items += '在 TEST 尝试启动模型、MCP、Amazon 采集或 VOC 任务，确认收到离线阻断；仅执行本地预览。'
} else {
    $items += '仅按下方用户填写的 live-call 记录执行外部验证，并确认实际输入范围和额度没有超出记录。'
}
$items += '退出并重新打开 TEST；每次启动都重新选择模式，确认本次 build、数据目录、实例 ID 和策略一致。'
$liveCallRecord = @(
    '## User-entered live-call record (required for live_validation)',
    '- Live-call service: [fill in service or N/A]',
    '- Live-call workflow/run ID: [fill in workflow or run ID]',
    '- Live-call selected input scope: [fill in exact input scope]',
    '- Live-call authorized ceiling: [fill in approved call/cost ceiling]',
    '- Live-call observed calls (user-entered, not machine-measured): [fill in after run]',
    '- Live-call result path (user-entered): [fill in report or artifact path]', ''
)
$liveCallSection = if ($status.external_effects.policy -eq 'live_validation') { $liveCallRecord } else { @() }
$content = @(
    '# 格物 TEST 人工验收', '',
    "- Version: $($candidateReport.version)",
    "- Run ID: $runId",
    "- Build ID: $($status.source.build_id)",
    "- Commit: $($status.source.commit)",
    "- Candidate report: $($candidateReportFile.FullName)",
    "- Candidate report SHA-256: $candidateReportHash",
    "- App manifest SHA-256: $($candidateReport.manifest_sha256)",
    "- Portable ZIP: $portableZip",
    "- Portable ZIP SHA-256: $($candidateReport.portable_sha256)",
    "- Portable sidecar: $portableSidecar",
    "- Portable sidecar SHA-256: $($candidateReport.portable_sidecar_sha256)",
    "- Data: $($status.data.path)",
    "- TEST policy: $($status.external_effects.policy)",
    "- Fixture: $($fixture.fixture_version)",
    "- Fixture manifest SHA-256: $fixtureHash",
    "- Automated report: $($automated.file.FullName)",
    "- Automated report SHA-256: $automatedHash",
    "- Browser report: $($browser.file.FullName)",
    "- Browser report SHA-256: $browserHash"
)
if ($status.external_effects.policy -eq 'offline') {
    $content += '- External calls: 0 expected (offline policy).'
} else {
    $content += '- External calls: use the user-entered record below; Run Center does not measure call counts.'
}
$content += ''
foreach ($line in $liveCallSection) { $content += $line }
$content += '逐项完成后把 `[ ]` 改为 `[x]`，发现问题时写明复现步骤和页面位置。'
$content += ''
foreach ($item in $items) { $content += "- [ ] $item"; $content += '' }
Set-Content -LiteralPath $path -Value ($content -join "`r`n") -Encoding UTF8
Write-Output "Manual checklist created: $path"
Write-Output "Build: $($status.source.build_id); fixture: $($fixture.fixture_version); offline report hash: $automatedHash; browser report hash: $browserHash"
