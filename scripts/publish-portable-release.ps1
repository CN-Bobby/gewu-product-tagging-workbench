[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ManualResult,
    [Parameter(Mandatory = $true)][string]$CandidateReport,
    [Parameter(Mandatory = $true)][string]$PromotionRecord,
    [string]$SourceRepository = 'CN-Bobby/gewu-product-tagging-workbench',
    [string]$BinaryRepository = $(if ($env:GEWU_UPDATE_REPOSITORY) { $env:GEWU_UPDATE_REPOSITORY } else { 'CN-Bobby/gewu-workbench-releases' })
)
$ErrorActionPreference = 'Stop'
function Get-Sha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Read-Json([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required evidence file is missing: $Path" }
    Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}
foreach ($repository in @($SourceRepository, $BinaryRepository)) {
    if ($repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw "Invalid GitHub repository name: $repository" }
}
$ManualResult = [IO.Path]::GetFullPath($ManualResult)
$CandidateReport = [IO.Path]::GetFullPath($CandidateReport)
$PromotionRecord = [IO.Path]::GetFullPath($PromotionRecord)
$result = Read-Json $ManualResult
$candidate = Read-Json $CandidateReport
$promotion = Read-Json $PromotionRecord
if ($result.schema -ne 2 -or $result.status -ne 'passed' -or $result.profile -ne 'test' -or
    $candidate.status -notin @('running_pending_manual_acceptance', 'activated_pending_acceptance', 'promoted') -or
    $result.version -ne $candidate.version -or $result.build_id -ne $candidate.build_id -or $result.commit -ne $candidate.commit -or
    $result.candidate_report_sha256 -ne (Get-Sha256 $CandidateReport) -or
    $result.candidate_report -ine $CandidateReport -or
    $result.portable_zip -ine [IO.Path]::GetFullPath([string]$candidate.portable_zip) -or
    $result.portable_sha256 -ne $candidate.portable_sha256 -or
    $result.portable_sidecar -ine [IO.Path]::GetFullPath([string]$candidate.portable_sidecar) -or
    $result.portable_sidecar_sha256 -ne $candidate.portable_sidecar_sha256 -or
    $result.app_manifest_sha256 -ne $candidate.manifest_sha256) {
    throw 'Manual acceptance result and frozen candidate report do not identify the same passing portable candidate.'
}
if ($promotion.schema -ne 1 -or $promotion.status -ne 'promoted' -or
    $promotion.version -ne $result.version -or $promotion.build_id -ne $result.build_id -or $promotion.commit -ne $result.commit -or
    [IO.Path]::GetFullPath([string]$promotion.manual_result) -ine $ManualResult -or
    [string]$promotion.manual_result_sha256 -ne (Get-Sha256 $ManualResult) -or
    [IO.Path]::GetFullPath([string]$promotion.candidate_report) -ine $CandidateReport -or
    [string]$promotion.candidate_report_sha256 -ne (Get-Sha256 $CandidateReport) -or
    [IO.Path]::GetFullPath([string]$promotion.portable_zip) -ine [IO.Path]::GetFullPath([string]$result.portable_zip) -or
    $promotion.portable_sha256 -ne $result.portable_sha256 -or
    [IO.Path]::GetFullPath([string]$promotion.portable_sidecar) -ine [IO.Path]::GetFullPath([string]$result.portable_sidecar) -or
    $promotion.portable_sidecar_sha256 -ne $result.portable_sidecar_sha256 -or
    $promotion.app_manifest_sha256 -ne $result.app_manifest_sha256) {
    throw 'PROD promotion record does not identify this manually accepted portable candidate.'
}
$expectedPromotionPath = Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath([string]$promotion.target_app))) 'PROMOTION.json'
if ($PromotionRecord -ine $expectedPromotionPath) { throw 'Promotion record is not adjacent to the promoted app.' }
if (@($result.checklist_items | Where-Object { $_.passed -ne $true }).Count -gt 0) {
    throw 'Passing manual acceptance result contains an unchecked checklist item.'
}
$checklistPath = [IO.Path]::GetFullPath([string]$result.manual_checklist)
$automatedPath = [IO.Path]::GetFullPath([string]$result.automated_report)
foreach ($evidencePath in @($checklistPath, $automatedPath)) {
    if (-not $evidencePath.StartsWith([IO.Path]::GetFullPath('K:\GeWuTest\Reports').TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $evidencePath -PathType Leaf)) {
        throw "Acceptance evidence is missing or outside TEST Reports: $evidencePath"
    }
}
if ((Get-Sha256 $checklistPath) -ne [string]$result.manual_checklist_sha256 -or
    (Get-Sha256 $automatedPath) -ne [string]$result.automated_report_sha256) {
    throw 'Manual checklist or automated TEST report changed after acceptance.'
}
$automated = Read-Json $automatedPath
if ($automated.status -ne 'passed' -or $automated.profile -ne 'test' -or $automated.commit -ne $result.commit -or
    $automated.dirty -ne $false -or $automated.external_requests -ne 0) {
    throw 'Automated TEST report is not a passing, clean, offline report for the accepted commit.'
}
$portableZip = [IO.Path]::GetFullPath([string]$result.portable_zip)
$sidecar = [IO.Path]::GetFullPath([string]$result.portable_sidecar)
$manifestPath = Join-Path ([IO.Path]::GetFullPath([string]$candidate.candidate_app)) 'build-manifest.json'
if (-not (Test-Path -LiteralPath $portableZip -PathType Leaf) -or
    -not (Test-Path -LiteralPath $sidecar -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Portable asset or candidate manifest is missing.' }
if ((Get-Sha256 $portableZip) -ne [string]$result.portable_sha256 -or
    (Get-Sha256 $sidecar) -ne [string]$result.portable_sidecar_sha256 -or
    (Get-Sha256 $manifestPath) -ne [string]$result.app_manifest_sha256) {
    throw 'A manually accepted candidate asset or evidence file changed.'
}
$expectedZipName = "GeWuWorkbench-$($result.version)-portable.zip"
if ((Split-Path -Leaf $portableZip) -ne $expectedZipName -or (Split-Path -Leaf $sidecar) -ne "$expectedZipName.sha256") {
    throw 'Portable ZIP or sidecar does not use the updater asset naming contract.'
}
$sidecarLine = (Get-Content -LiteralPath $sidecar -Raw -Encoding UTF8).Trim()
if ($sidecarLine -ne "$($result.portable_sha256)  $expectedZipName") { throw 'Portable SHA-256 sidecar does not match the accepted ZIP.' }
$manifest = Read-Json $manifestPath
if ($manifest.version -ne $result.version -or $manifest.build_id -ne $result.build_id -or
    $manifest.commit -ne $result.commit -or $manifest.release_status -ne 'pending_acceptance' -or
    $manifest.dirty -ne $false -or $manifest.build_mode -ne 'candidate' -or $manifest.storage_mode -ne 'user-selected') {
    throw 'Candidate app manifest does not match the accepted beta candidate.'
}
$tag = "v$($result.version)"
$compare = & gh api "repos/$SourceRepository/compare/main...$($result.commit)" --jq '.ahead_by'
if ($LASTEXITCODE -ne 0) { throw 'Could not verify source commit against GitHub main; no release was published.' }
if ([int]$compare -ne 0) { throw 'The accepted source commit is not contained in GitHub main; no release was published.' }
$notes = @"
# 格物工作台 $($result.version)

- 源码仓库：$SourceRepository
- 源码 main commit：$($result.commit)
- Build ID：$($result.build_id)
- 人工验收 Run ID：$($result.run_id)
- 便携包 SHA-256：$($result.portable_sha256)

本 Release 上传的是 TEST 人工验收通过的原始便携 ZIP 与 SHA-256 侧车；发布过程不会重新构建或重新压缩。
"@
& gh release create $tag $portableZip $sidecar --repo $BinaryRepository --title "格物工作台 $($result.version)" --prerelease --notes $notes
if ($LASTEXITCODE -ne 0) { throw 'GitHub Release creation failed. Check repository access and whether this version tag already exists.' }
Write-Output "Published exact accepted portable ZIP to $BinaryRepository Release $tag"
Write-Output "Source main commit: $($result.commit)"
Write-Output "Portable SHA-256: $($result.portable_sha256)"

