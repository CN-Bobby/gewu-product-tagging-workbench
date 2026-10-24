[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ManualResult,
    [Parameter(Mandatory = $true)][string]$BackupEvidence,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json')
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$rcRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
$expectedConfig = Join-Path $rcRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfig) { throw "Run Center config is pinned to $expectedConfig." }
Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Run Center config is missing: $ConfigPath" }
Assert-DgNoReparseAncestors $ConfigPath 'Run Center config' | Out-Null
$config = Read-DgJson $ConfigPath 'Run Center config'
$reportsRoot = 'K:\GeWuDev\Reports'
$prodRoot = 'K:\Bobbytagging'
$prodData = 'K:\Bobbytagging\Data'
$prodCache = 'K:\Bobbytagging\Cache'
if ([IO.Path]::GetFullPath([string]$config.profiles.DEV.reports).TrimEnd('\') -ine $reportsRoot -or
    [IO.Path]::GetFullPath([string]$config.profiles.DEV.data).TrimEnd('\') -ine 'K:\GeWuDev\Data' -or
    [IO.Path]::GetFullPath([string]$config.profiles.DEV.cache).TrimEnd('\') -ine 'K:\GeWuDev\Cache' -or
    [IO.Path]::GetFullPath([string]$config.profiles.PROD.data).TrimEnd('\') -ine $prodData -or
    [IO.Path]::GetFullPath([string]$config.profiles.PROD.cache).TrimEnd('\') -ine $prodCache) {
    throw 'Run Center DEV/PROD reports and data/cache paths do not match the fixed environment contract.'
}
$backup = Test-DgProdBackupReadiness $BackupEvidence 'K:\Bobbytagging\Backups' $prodData $prodCache
$manual = Test-DgManualAcceptance $ManualResult $reportsRoot (Join-Path $sourceRoot 'builds') 'K:\GeWuDev\Data'
$candidate = $manual.candidate
$versionMatch = [regex]::Match($candidate.version, '^0\.5\.0-beta\.(?<n>[1-9][0-9]*)$')
if (-not $versionMatch.Success) { throw 'Unsupported frozen DEV beta version.' }
if ([string]$config.profiles.PROD.app -notmatch '^K:\\Bobbytagging\\(?:app|Candidate-beta\.[1-9][0-9]*\\app)$') {
    throw 'Current PROD app is outside the known immutable app locations.'
}
foreach ($path in @($prodRoot, $prodData, $prodCache)) {
    Assert-DgNoReparseAncestors $path "PROD path $path" | Out-Null
}
Assert-DgPlainPath $prodRoot 'PROD root' -Directory | Out-Null
Assert-DgPlainPath $prodData 'PROD data' -Directory | Out-Null
Assert-DgPlainPath $prodCache 'PROD cache' -Directory | Out-Null
$targetName = "Candidate-beta.$($versionMatch.Groups['n'].Value)"
$targetRoot = Join-Path $prodRoot $targetName
$targetApp = Join-Path $targetRoot 'app'
if ((Get-DgCanonicalPath $targetRoot 'PROD candidate target') -ieq 'K:\Bobbytagging\Candidate-beta.8' -or
    (Get-DgCanonicalPath $targetApp 'PROD candidate app') -ieq (Get-DgCanonicalPath 'K:\Bobbytagging\app' 'Protected stable app')) {
    throw 'Computed target overlaps a protected existing PROD app.'
}
if (Test-Path -LiteralPath $targetRoot) { throw "PROD candidate already exists; refusing overwrite: $targetRoot" }
if (Test-Path -LiteralPath $targetApp) { throw "PROD candidate app already exists; refusing overwrite: $targetApp" }
$prodPort = [int]$config.profiles.PROD.port
try { $listener = @(Get-NetTCPConnection -LocalPort $prodPort -State Listen -ErrorAction Stop) }
catch { throw "Cannot verify that PROD port $prodPort is free: $($_.Exception.Message)" }
if ($listener.Count -gt 0) { throw "PROD port $prodPort is occupied; no app or config was changed." }

$lockPath = Join-Path $rcRoot 'maintenance.lock'
$lock = $null
$stageRoot = Join-Path $prodRoot ('.' + $targetName + '.stage-' + [Guid]::NewGuid().ToString('N'))
try {
    try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Run Center maintenance lock is busy; PROD was not changed.' }
    if (Test-Path -LiteralPath $targetRoot) { throw 'PROD target appeared while waiting for the maintenance lock; no overwrite performed.' }
    New-Item -ItemType Directory -Path (Join-Path $stageRoot 'app') -ErrorAction Stop | Out-Null
    Expand-DgPortableZip $candidate.portable_zip (Join-Path $stageRoot 'app') $candidate.manifest $candidate.manifest_sha256 'DEV portable candidate'
    $manifestPath = Join-Path (Join-Path $stageRoot 'app') 'build-manifest.json'
    $manifest = Read-DgJson $manifestPath 'Extracted portable manifest'
    if ((Get-DgSha256 $manifestPath) -ne $candidate.manifest_sha256) { throw 'Extracted portable manifest hash differs from DEV acceptance.' }
    Test-DgAppTree (Join-Path $stageRoot 'app') $manifest $candidate.manifest_sha256 'Extracted portable app'
    $promotion = [ordered]@{
        schema = 1; status = 'promoted_pending_prod_verification'; created_at = [DateTime]::UtcNow.ToString('o')
        profile = 'prod'; version = $candidate.version; build_id = $candidate.build_id; commit = $candidate.commit
        source_sha256 = $candidate.source_sha256; manual_result = $manual.result_path; manual_result_sha256 = $manual.result_sha256
        candidate_report = $candidate.report_path; candidate_report_sha256 = $candidate.report_sha256
        manual_checklist = $manual.checklist_path; manual_checklist_sha256 = Get-DgSha256 $manual.checklist_path
        prod_backup_report = $backup.report_path; prod_backup_report_sha256 = $backup.report_sha256
        prod_database_backup = $backup.database_backup; prod_reasonix_backup = $backup.reasonix_backup
        prod_restore_root = $backup.restore_root; prod_restored_database = $backup.restored_database
        prod_reasonix_session_ids = @($backup.session_ids)
        candidate_app = $candidate.candidate_app; portable_zip = $candidate.portable_zip; portable_sha256 = $candidate.portable_sha256
        portable_sidecar = $candidate.portable_sidecar; portable_sidecar_sha256 = $candidate.portable_sidecar_sha256
        app_manifest_sha256 = $candidate.manifest_sha256; target_app = $targetApp
        run_center_config = $expectedConfig; prod_data_path = $prodData; prod_cache_path = $prodCache
    }
    $promotionPath = Join-Path $stageRoot 'PROMOTION.json'
    $promotion | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $promotionPath -Encoding UTF8
    Assert-DgNoReparseAncestors $stageRoot 'PROD staging path' | Out-Null
    [IO.Directory]::Move($stageRoot, $targetRoot)
    $stageRoot = $null

    # Replace only config.profiles.PROD.app, preserving the exact semantics of every other key.
    $raw = [IO.File]::ReadAllBytes($expectedConfig)
    $bom = $raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF
    $offset = if ($bom) { 3 } else { 0 }
    $text = [Text.Encoding]::UTF8.GetString($raw, $offset, $raw.Length - $offset)
    $profileMatches = [regex]::Matches($text, '(?s)"PROD"\s*:\s*\{(?<body>[^{}]*)\}')
    if ($profileMatches.Count -ne 1) { throw 'Could not uniquely locate config.profiles.PROD; candidate remains immutable and config is unchanged.' }
    $profile = $profileMatches[0]
    $appMatches = [regex]::Matches($profile.Groups['body'].Value, '(?m)(?<prefix>^[\t ]*"app"[\t ]*:[\t ]*")[^"]*(?<suffix>")')
    if ($appMatches.Count -ne 1) { throw 'Could not uniquely locate config.profiles.PROD.app; candidate remains immutable and config is unchanged.' }
    $app = $appMatches[0]
    $replacement = $app.Groups['prefix'].Value + $targetApp + $app.Groups['suffix'].Value
    $body = $profile.Groups['body'].Value.Substring(0, $app.Index) + $replacement + $profile.Groups['body'].Value.Substring($app.Index + $app.Length)
    $newText = $text.Substring(0, $profile.Groups['body'].Index) + $body + $text.Substring($profile.Groups['body'].Index + $profile.Groups['body'].Length)
    $expected = $config | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    $expected.profiles.PROD.app = $targetApp
    $parsed = $newText | ConvertFrom-Json
    if ((ConvertTo-Json $parsed -Depth 100 -Compress) -ne (ConvertTo-Json $expected -Depth 100 -Compress)) {
        throw 'PROD config edit would change values other than profiles.PROD.app; config is unchanged.'
    }
    $newBytes = [Text.UTF8Encoding]::new($false).GetBytes($newText)
    if ($bom) { $newBytes = [byte[]](@(0xEF,0xBB,0xBF) + $newBytes) }
    $temp = $expectedConfig + '.dev-promote-' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllBytes($temp, $newBytes)
    try { [IO.File]::Replace($temp, $expectedConfig, $null) }
    finally { if (Test-Path -LiteralPath $temp -PathType Leaf) { Remove-Item -LiteralPath $temp -Force } }
    $saved = Read-DgJson $expectedConfig 'Updated Run Center config'
    if ([IO.Path]::GetFullPath([string]$saved.profiles.PROD.app).TrimEnd('\') -ine $targetApp -or
        [IO.Path]::GetFullPath([string]$saved.profiles.PROD.data).TrimEnd('\') -ine $prodData -or
        [IO.Path]::GetFullPath([string]$saved.profiles.PROD.cache).TrimEnd('\') -ine $prodCache) {
        throw 'PROD app path update failed verification or changed protected data/cache configuration.'
    }
    Write-Output "DEV-accepted candidate promoted to PROD app path: $targetApp"
    Write-Output "Promotion evidence: $(Join-Path $targetRoot 'PROMOTION.json')"
    Write-Output 'PROD is not yet verified or published. Start via Run Center and verify live PROD identity before publication.'
} finally {
    if ($stageRoot -and (Test-Path -LiteralPath $stageRoot)) {
        Write-Warning "Incomplete uniquely named staging material was preserved for inspection: $stageRoot"
    }
    if ($lock) { $lock.Dispose() }
}
