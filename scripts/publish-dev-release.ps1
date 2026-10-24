[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ManualResult,
    [Parameter(Mandatory = $true)][string]$CandidateReport,
    [Parameter(Mandatory = $true)][string]$PromotionRecord,
    [string]$SourceRepository = 'CN-Bobby/gewu-product-tagging-workbench',
    [string]$ReleaseRepository = 'CN-Bobby/gewu-product-tagging-workbench',
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json')
)
$ErrorActionPreference = 'Stop'
foreach ($repository in @($SourceRepository, $ReleaseRepository)) {
    if ($repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw "Invalid GitHub repository: $repository" }
}
$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$origin = (& git -C $sourceRoot remote get-url origin 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $origin -notmatch '^(?:https://github\.com/|git@github\.com:)(?<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$') {
    throw 'Could not bind the publication target to this checkout GitHub origin.'
}
$originRepository = $Matches['repo']
if ($SourceRepository -ine $originRepository -or $ReleaseRepository -ine $originRepository) {
    throw 'DEV publication is pinned to the exact GitHub origin repository for both source main and Release.'
}
$rcRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
$expectedConfig = Join-Path $rcRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfig) { throw "Run Center config is pinned to $expectedConfig." }
Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
$config = Read-DgJson $expectedConfig 'Run Center config'
$reportsRoot = 'K:\GeWuDev\Reports'
$manual = Test-DgManualAcceptance $ManualResult $reportsRoot (Join-Path $sourceRoot 'builds') 'K:\GeWuDev\Data'
$candidate = Test-DgCandidateBundle $CandidateReport $reportsRoot (Join-Path $sourceRoot 'builds')
if ($manual.candidate.report_sha256 -ne $candidate.report_sha256) { throw 'Manual acceptance and candidate report do not bind the same DEV candidate.' }
$promotionPath = Get-DgCanonicalExistingPath $PromotionRecord 'PROD promotion record'
$null = Assert-DgUnderRoot $promotionPath 'K:\Bobbytagging' 'PROD promotion record'
Assert-DgPlainPath $promotionPath 'PROD promotion record' | Out-Null
$promotion = Read-DgJson $promotionPath 'PROD promotion record'
$promotionHash = Get-DgSha256 $promotionPath
$backupGate = Test-DgProdBackupReadiness ([string]$promotion.prod_backup_report) 'K:\Bobbytagging\Backups' 'K:\Bobbytagging\Data' 'K:\Bobbytagging\Cache'
if ($promotion.schema -ne 1 -or $promotion.status -ne 'promoted_pending_prod_verification' -or
    $promotion.version -ne $candidate.version -or $promotion.build_id -ne $candidate.build_id -or
    $promotion.commit -ne $candidate.commit -or $promotion.source_sha256 -ne $candidate.source_sha256 -or
    [IO.Path]::GetFullPath([string]$promotion.manual_result) -ine $manual.result_path -or
    [string]$promotion.manual_result_sha256 -ne $manual.result_sha256 -or
    [IO.Path]::GetFullPath([string]$promotion.candidate_report) -ine $candidate.report_path -or
    [string]$promotion.candidate_report_sha256 -ne $candidate.report_sha256 -or
    [string]$promotion.portable_sha256 -ne $candidate.portable_sha256 -or
    [string]$promotion.app_manifest_sha256 -ne $candidate.manifest_sha256 -or
    [string]$promotion.prod_backup_report_sha256 -ne $backupGate.report_sha256 -or
    [IO.Path]::GetFullPath([string]$promotion.prod_database_backup) -ine $backupGate.database_backup -or
    [IO.Path]::GetFullPath([string]$promotion.prod_reasonix_backup) -ine $backupGate.reasonix_backup -or
    [IO.Path]::GetFullPath([string]$promotion.prod_restore_root) -ine $backupGate.restore_root -or
    [IO.Path]::GetFullPath([string]$promotion.prod_restored_database) -ine $backupGate.restored_database) {
    throw 'PROD promotion record does not bind the DEV-accepted candidate.'
}
$app = Get-DgCanonicalExistingPath ([string]$promotion.target_app) 'Promoted PROD app'
if ($app -ine (Get-DgCanonicalPath ([string]$config.profiles.PROD.app) 'Configured PROD app') -or
    $app -notmatch '^K:\\Bobbytagging\\Candidate-beta\.[1-9][0-9]*\\app$' -or
    [IO.Path]::GetFullPath([string]$config.profiles.PROD.data).TrimEnd('\') -ine 'K:\Bobbytagging\Data' -or
    [IO.Path]::GetFullPath([string]$config.profiles.PROD.cache).TrimEnd('\') -ine 'K:\Bobbytagging\Cache') {
    throw 'Configured PROD app/data/cache does not match the immutable promoted target and protected production paths.'
}
Assert-DgNoReparseAncestors $app 'Promoted PROD app' | Out-Null
$manifestPath = Join-Path $app 'build-manifest.json'
$manifest = Read-DgJson $manifestPath 'Promoted PROD manifest'
if ((Get-DgSha256 $manifestPath) -ne $candidate.manifest_sha256 -or
    $manifest.version -ne $candidate.version -or $manifest.build_id -ne $candidate.build_id -or
    $manifest.commit -ne $candidate.commit -or $manifest.source_sha256 -ne $candidate.source_sha256) {
    throw 'Promoted PROD app manifest changed or does not match DEV acceptance.'
}
Test-DgAppTree $app $manifest $candidate.manifest_sha256 'Promoted PROD app'

$runtimeRecordPath = Join-Path $rcRoot 'runtime\PROD.json'
$runtimeRecord = Read-DgJson $runtimeRecordPath 'Run Center live PROD process record'
if ($runtimeRecord.profile -ne 'PROD' -or $runtimeRecord.build_id -ne $candidate.build_id -or
    $runtimeRecord.commit -ne $candidate.commit -or [IO.Path]::GetFullPath([string]$runtimeRecord.data) -ine 'K:\Bobbytagging\Data' -or
    [IO.Path]::GetFullPath([string]$runtimeRecord.cache) -ine 'K:\Bobbytagging\Cache' -or
    [IO.Path]::GetFullPath([string]$runtimeRecord.prod_verification_path).StartsWith('K:\Bobbytagging\', [StringComparison]::OrdinalIgnoreCase) -ne $true) {
    throw 'Run Center has no matching live PROD verification record.'
}
$verifiedProd = Test-DgProdVerification ([string]$runtimeRecord.prod_verification_path) $promotionPath $runtimeRecordPath $app 'K:\Bobbytagging\Data' 'K:\Bobbytagging\Cache'
$verification = $verifiedProd.verification
$verificationPath = $verifiedProd.verification_path
$liveUrl = [string]$config.profiles.PROD.url
try {
    $health = Invoke-RestMethod -Uri "$liveUrl/api/health" -TimeoutSec 3
    $runtime = Invoke-RestMethod -Uri "$liveUrl/api/runtime/status" -TimeoutSec 3
} catch { throw "Live PROD identity could not be read: $($_.Exception.Message)" }
if ($health.status -ne 'ok' -or $health.application -ne 'product-tagging-workbench' -or
    $health.version -ne $candidate.version -or $health.build_id -ne $candidate.build_id -or
    $health.commit -ne $candidate.commit -or $health.source_sha256 -ne $candidate.source_sha256 -or
    $health.manifest_sha256 -ne $candidate.manifest_sha256 -or
    (Get-DgCanonicalPath $health.storage 'PROD live data path') -ine 'K:\Bobbytagging\Data' -or
    $runtime.profile -ne 'prod' -or $runtime.instance_id -ne 'PROD' -or
    $runtime.source.build_id -ne $candidate.build_id -or $runtime.source.commit -ne $candidate.commit -or
    $runtime.source.source_sha256 -ne $candidate.source_sha256 -or
    (Get-DgCanonicalPath $runtime.data.path 'PROD runtime data path') -ine 'K:\Bobbytagging\Data') {
    throw 'Current live PROD health/runtime identity no longer matches the verified accepted build.'
}
$port = [int]$config.profiles.PROD.port
try { $listenerPid = [int](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | Select-Object -First 1 -ExpandProperty OwningProcess) }
catch { throw 'Could not verify the live PROD listener PID.' }
$recordedRuntimeProcess = @($runtimeRecord.processes | Where-Object role -eq 'app-runtime') | Select-Object -First 1
if (-not $recordedRuntimeProcess -or [int]$recordedRuntimeProcess.pid -ne $listenerPid -or
    [int]$verification.process.pid -ne $listenerPid) { throw 'Live PROD listener is not the Run Center verified embedded runtime process.' }
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
if (-not $process -or [IO.Path]::GetFullPath([string]$process.ExecutablePath) -ine [IO.Path]::GetFullPath([string]$recordedRuntimeProcess.executable) -or
    [string]$process.CommandLine -notlike "*$($recordedRuntimeProcess.identity_token)*") {
    throw 'Live PROD process executable/command identity changed; no GitHub write was made.'
}

# GitHub is read only until all local manual, bundle, promotion, and live PROD gates pass.
$tag = "v$($candidate.version)"
$compareOutput = & gh api "repos/$SourceRepository/compare/main...$($candidate.commit)" --jq '.ahead_by' 2>&1
if ($LASTEXITCODE -ne 0 -or [int]($compareOutput | Select-Object -Last 1) -ne 0) {
    throw 'Accepted commit is not verified as contained in GitHub main; no tag or Release was created.'
}
$releasePath = "repos/$ReleaseRepository/releases/tags/$tag"
$oldPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$releaseOutput = & gh api $releasePath --jq '.id' 2>&1
$releaseExit = $LASTEXITCODE
$ErrorActionPreference = $oldPreference
if ($releaseExit -eq 0) { throw "GitHub Release $tag already exists; immutable attachments will not be overwritten." }
if (($releaseOutput -join "`n") -notmatch 'HTTP 404: Not Found') { throw "Could not safely determine whether Release $tag exists: $($releaseOutput -join ' ')" }
$refPath = "repos/$SourceRepository/git/ref/tags/$tag"
$ErrorActionPreference = 'Continue'
$refOutput = & gh api $refPath --jq '.object.type + ":" + .object.sha' 2>&1
$refExit = $LASTEXITCODE
$ErrorActionPreference = $oldPreference
if ($refExit -eq 0) {
    $ref = [string]($refOutput | Select-Object -Last 1)
    if ($ref -ne "commit:$($candidate.commit)") { throw "Immutable tag $tag already exists but does not point directly to the accepted commit; no release was created." }
} elseif (($refOutput -join "`n") -notmatch 'HTTP 404: Not Found') {
    throw "Could not safely determine whether tag $tag already exists: $($refOutput -join ' ')"
} else {
    & gh api "repos/$SourceRepository/git/refs" -f "ref=refs/tags/$tag" -f "sha=$($candidate.commit)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the immutable Git tag at the exact accepted commit.' }
}
$ErrorActionPreference = 'Continue'
$releaseOutput = & gh api $releasePath --jq '.id' 2>&1
$releaseExit = $LASTEXITCODE
$ErrorActionPreference = $oldPreference
if ($releaseExit -eq 0) { throw "GitHub Release $tag appeared during publication; no attachments were uploaded." }
if (($releaseOutput -join "`n") -notmatch 'HTTP 404: Not Found') { throw "Could not re-check Release $tag before creation: $($releaseOutput -join ' ')" }
$notes = @(
    "# 格物工作台 $($candidate.version)",
    '',
    "- DEV 人工验收 Run ID：$($manual.result.run_id)",
    "- 源码 main commit：$($candidate.commit)",
    "- Build ID：$($candidate.build_id)",
    "- PROD 实时核验文件：$verificationPath",
    "- 便携 ZIP SHA-256：$($candidate.portable_sha256)",
    '',
    '此 Release 上传的是 DEV 人工验收并经 Run Center 核验为当前 PROD 实例的原始便携 ZIP 与 SHA-256 侧车，不重新构建或重压缩。'
) -join "`n"
& gh release create $tag $candidate.portable_zip $candidate.portable_sidecar --repo $ReleaseRepository --title "格物工作台 $($candidate.version)" --prerelease --verify-tag --target $candidate.commit --notes $notes
if ($LASTEXITCODE -ne 0) { throw 'GitHub Release creation failed; inspect the tag and repository state before retrying.' }
Write-Output "Published verified PROD build to $ReleaseRepository Release $tag"
Write-Output "Commit: $($candidate.commit); Build ID: $($candidate.build_id); ZIP SHA-256: $($candidate.portable_sha256)"
