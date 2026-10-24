Set-StrictMode -Version 2.0
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop

function Get-DgSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Assert-DgHash([object]$Value, [string]$Label) {
    $text = [string]$Value
    if ($text -notmatch '^[0-9a-fA-F]{64}$') { throw "$Label must be a 64-character SHA-256." }
    return $text.ToLowerInvariant()
}

function Get-DgCanonicalExistingPath([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw "$Label must be an absolute path."
    }
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    $full = [IO.Path]::GetFullPath($resolved.ProviderPath).TrimEnd('\')
    Assert-DgNoReparseAncestors $full $Label | Out-Null
    return $full
}

function Get-DgCanonicalPath([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw "$Label must be an absolute path."
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Assert-DgUnderRoot([string]$Path, [string]$Root, [string]$Label) {
    $canonicalPath = Get-DgCanonicalPath $Path $Label
    $canonicalRoot = Get-DgCanonicalPath $Root "$Label root"
    if ($canonicalPath -ne $canonicalRoot -and
        -not $canonicalPath.StartsWith($canonicalRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside its approved root: $canonicalPath"
    }
    return $canonicalPath
}

function Assert-DgPlainPath([string]$Path, [string]$Label, [switch]$Directory) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($Directory) {
        if (-not $item.PSIsContainer) { throw "$Label must be a directory: $Path" }
    } elseif ($item.PSIsContainer) {
        throw "$Label must be a file: $Path"
    }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$Label must not be a reparse point: $Path"
    }
    return $item
}

function Assert-DgNoReparseAncestors([string]$Path, [string]$Label) {
    $canonical = Get-DgCanonicalPath $Path $Label
    $current = [IO.Path]::GetPathRoot($canonical)
    $relative = $canonical.Substring($current.Length)
    foreach ($part in @($relative -split '[\\/]') | Where-Object { $_ }) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label traverses a reparse point: $current" }
        }
    }
    return $canonical
}

function Read-DgJson([string]$Path, [string]$Label) {
    Assert-DgPlainPath $Path $Label | Out-Null
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "$Label is not valid JSON: $Path" }
}

function Get-DgManifestFileMap($Manifest, [string]$Label) {
    if (-not $Manifest.files) { throw "$Label has no file inventory." }
    $map = @{}
    foreach ($property in $Manifest.files.PSObject.Properties) {
        $relative = [string]$property.Name
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Contains('\') -or
            [IO.Path]::IsPathRooted($relative) -or $relative -match '[:<>"|?*]' -or
            $relative.StartsWith('/') -or $relative.EndsWith('/')) {
            throw "$Label has an unsafe manifest path: $relative"
        }
        foreach ($part in $relative.Split('/')) {
            if ($part -in @('', '.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
                $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') {
                throw "$Label has an unsafe manifest path: $relative"
            }
        }
        if ($relative -ieq 'build-manifest.json') { throw "$Label must not list its own manifest." }
        $hash = Assert-DgHash $property.Value "$Label files[$relative]"
        if ($map.ContainsKey($relative)) { throw "$Label has a duplicate file path: $relative" }
        $map[$relative] = $hash
    }
    if ($map.Count -eq 0) { throw "$Label file inventory is empty." }
    return ,$map
}

function Test-DgAppTree([string]$AppPath, $Manifest, [string]$ExpectedManifestHash, [string]$Label) {
    Assert-DgPlainPath $AppPath $Label -Directory | Out-Null
    $manifestPath = Join-Path $AppPath 'build-manifest.json'
    Assert-DgPlainPath $manifestPath "$Label build manifest" | Out-Null
    if ((Get-DgSha256 $manifestPath) -ne $ExpectedManifestHash) { throw "$Label build manifest hash changed." }

    $map = Get-DgManifestFileMap $Manifest $Label
    $actual = @{}
    foreach ($directory in Get-ChildItem -LiteralPath $AppPath -Recurse -Directory -Force -ErrorAction Stop) {
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label contains a reparse-point directory: $($directory.FullName)" }
    }
    foreach ($file in Get-ChildItem -LiteralPath $AppPath -Recurse -File -Force -ErrorAction Stop) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label contains a reparse-point file: $($file.FullName)" }
        $relative = $file.FullName.Substring($AppPath.Length).TrimStart('\').Replace('\', '/')
        if ($relative -ieq 'build-manifest.json') {
            if ($relative -cne 'build-manifest.json') { throw "$Label manifest filename is not exact." }
            continue
        }
        if ($actual.ContainsKey($relative)) { throw "$Label contains a duplicate file path: $relative" }
        $actual[$relative] = [pscustomobject]@{ path = $file.FullName; relative = $relative }
    }
    if ($actual.Count -ne $map.Count) { throw "$Label file count does not match its manifest." }
    foreach ($relative in $map.Keys) {
        if (-not $actual.ContainsKey($relative)) { throw "$Label is missing manifest file: $relative" }
        if ($actual[$relative].relative -cne $relative) { throw "$Label file path casing differs from its manifest: $relative" }
        if ((Get-DgSha256 $actual[$relative].path) -ne $map[$relative]) { throw "$Label file hash does not match its manifest: $relative" }
    }
}

function Get-DgZipEntryHash($Entry) {
    $stream = $Entry.Open()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Test-DgPortableZip([string]$ZipPath, $Manifest, [string]$ManifestHash, [string]$Label) {
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $map = Get-DgManifestFileMap $Manifest $Label
        $entries = @{}
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('\') -or $name.StartsWith('/') -or
                [IO.Path]::IsPathRooted($name) -or $name -match '[:<>"|?*]' -or $name.EndsWith('/')) {
                throw "$Label contains an unsafe ZIP path: $name"
            }
            foreach ($part in $name.Split('/')) {
                if ($part -in @('', '.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
                    $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') {
                    throw "$Label contains an unsafe ZIP path: $name"
                }
            }
            if ($entries.ContainsKey($name)) { throw "$Label contains a duplicate ZIP entry: $name" }
            $entries[$name] = $entry
        }
        if (-not $entries.ContainsKey('build-manifest.json')) { throw "$Label is missing root build-manifest.json." }
        if ((Get-DgZipEntryHash $entries['build-manifest.json']) -ne $ManifestHash) {
            throw "$Label build manifest hash does not match the accepted candidate."
        }
        if ($entries.Count -ne ($map.Count + 1)) { throw "$Label contains files outside the manifest or is incomplete." }
        foreach ($relative in $map.Keys) {
            if (-not $entries.ContainsKey($relative)) { throw "$Label is missing manifest file: $relative" }
            if ($entries[$relative].FullName -cne $relative) { throw "$Label ZIP path casing differs from the manifest: $relative" }
            if ((Get-DgZipEntryHash $entries[$relative]) -ne $map[$relative]) { throw "$Label file hash does not match manifest: $relative" }
        }
    } finally { $archive.Dispose() }
}

function Expand-DgPortableZip([string]$ZipPath, [string]$AppPath, $Manifest, [string]$ManifestHash, [string]$Label) {
    $zipFile = Get-DgCanonicalExistingPath $ZipPath "$Label ZIP"
    $appRoot = Get-DgCanonicalExistingPath $AppPath "$Label app destination"
    Assert-DgPlainPath $appRoot "$Label app destination" -Directory | Out-Null
    Assert-DgNoReparseAncestors $appRoot "$Label app destination" | Out-Null
    Test-DgPortableZip $zipFile $Manifest $ManifestHash $Label
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $archive = [IO.Compression.ZipFile]::OpenRead($zipFile)
    try {
        foreach ($entry in $archive.Entries) {
            $relative = $entry.FullName.Replace('/', '\')
            $destination = [IO.Path]::GetFullPath((Join-Path $appRoot $relative))
            $null = Assert-DgUnderRoot $destination $appRoot "$Label extracted file"
            $parent = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Assert-DgPlainPath $parent "$Label extracted directory" -Directory | Out-Null
            Assert-DgNoReparseAncestors $parent "$Label extracted directory" | Out-Null
            if (Test-Path -LiteralPath $destination) { throw "$Label extraction would overwrite an existing file: $destination" }
            $input = $entry.Open()
            $output = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $input.CopyTo($output); $output.Flush($true) }
            finally { $output.Dispose(); $input.Dispose() }
        }
    } finally { $archive.Dispose() }
    Test-DgAppTree $appRoot $Manifest $ManifestHash "$Label extracted app"
}

function Test-DgOfflineReport([string]$ReportPath, [string]$ExpectedHash, [string]$ReportsRoot, [string]$Commit) {
    $path = Get-DgCanonicalExistingPath $ReportPath 'Automated offline report'
    $null = Assert-DgUnderRoot $path $ReportsRoot 'Automated offline report'
    Assert-DgPlainPath $path 'Automated offline report' | Out-Null
    if ((Get-DgSha256 $path) -ne (Assert-DgHash $ExpectedHash 'automated_report_sha256')) {
        throw 'Automated offline report SHA-256 changed.'
    }
    $report = Read-DgJson $path 'Automated offline report'
    if ($report.status -ne 'passed' -or $report.profile -ne 'test' -or
        ([string]$report.commit).ToLowerInvariant() -ne $Commit -or $report.dirty -ne $false -or
        $report.external_requests -ne 0 -or [string]$report.report_scope -notlike '*offline*') {
        throw 'Automated report must prove a passing, clean, same-commit offline check with zero external requests.'
    }
    return $report
}

function Test-DgCandidateBundle([string]$ReportPath, [string]$ReportsRoot, [string]$BuildsRoot) {
    $reportsRoot = Get-DgCanonicalPath $ReportsRoot 'Reports root'
    $buildsRoot = Get-DgCanonicalPath $BuildsRoot 'Builds root'
    Assert-DgNoReparseAncestors $reportsRoot 'Reports root' | Out-Null
    Assert-DgNoReparseAncestors $buildsRoot 'Builds root' | Out-Null
    $reportFile = Get-DgCanonicalExistingPath $ReportPath 'Candidate report'
    $null = Assert-DgUnderRoot $reportFile $reportsRoot 'Candidate report'
    Assert-DgPlainPath $reportFile 'Candidate report' | Out-Null
    $report = Read-DgJson $reportFile 'Candidate report'
    if ($report.schema -ne 1 -or $report.status -ne 'built_pending_dev_acceptance' -or
        $report.profile -ne 'dev' -or $report.dirty -ne $false) {
        throw 'Candidate report must identify a clean DEV build awaiting manual acceptance.'
    }
    $commit = ([string]$report.commit).ToLowerInvariant()
    if ($commit -notmatch '^[0-9a-f]{40}$') { throw 'Candidate commit must be a full 40-character Git commit id.' }
    $version = [string]$report.version
    if ($version -notmatch '^0\.5\.0-beta\.[1-9][0-9]*$' -or
        [string]$report.build_id -ne ($version + '+' + $commit.Substring(0, 12))) {
        throw 'Candidate version/build_id does not match its full commit.'
    }

    $candidateApp = Get-DgCanonicalExistingPath ([string]$report.candidate_app) 'Candidate app'
    $null = Assert-DgUnderRoot $candidateApp $buildsRoot 'Candidate app'
    if ((Split-Path -Leaf $candidateApp) -ine 'app') { throw 'Candidate app must be named app.' }
    $candidateBuildRoot = [IO.Path]::GetFullPath((Split-Path -Parent $candidateApp)).TrimEnd('\')
    $null = Assert-DgUnderRoot $candidateBuildRoot $buildsRoot 'Candidate build directory'
    if ([IO.Path]::GetFullPath((Split-Path -Parent $candidateBuildRoot)).TrimEnd('\') -ine $buildsRoot -or
        (Split-Path -Leaf $candidateBuildRoot) -ine ($version + '-candidate.' + $commit.Substring(0, 12))) {
        throw 'Candidate build must be a direct child of builds and named from its exact version and commit.'
    }
    Assert-DgPlainPath $candidateBuildRoot 'Candidate build directory' -Directory | Out-Null
    Assert-DgPlainPath $candidateApp 'Candidate app' -Directory | Out-Null

    $manifestPath = Join-Path $candidateApp 'build-manifest.json'
    Assert-DgPlainPath $manifestPath 'Candidate build manifest' | Out-Null
    $manifestHash = Get-DgSha256 $manifestPath
    if ((Assert-DgHash $report.manifest_sha256 'manifest_sha256') -ne $manifestHash -or
        (Assert-DgHash $report.build_manifest_sha256 'build_manifest_sha256') -ne $manifestHash) {
        throw 'Candidate report build manifest hash does not match the candidate app.'
    }
    $manifest = Read-DgJson $manifestPath 'Candidate build manifest'
    if ($manifest.version -ne $version -or $manifest.build_id -ne $report.build_id -or
        ([string]$manifest.commit).ToLowerInvariant() -ne $commit -or $manifest.dirty -ne $false -or
        $manifest.build_mode -ne 'candidate' -or $manifest.storage_mode -ne 'user-selected' -or
        $manifest.release_status -ne 'pending_acceptance' -or
        (Assert-DgHash $manifest.source_sha256 'manifest.source_sha256') -ne
            (Assert-DgHash $report.source_sha256 'candidate source_sha256')) {
        throw 'Candidate manifest does not match the clean DEV candidate report identity.'
    }
    $manifestMap = Get-DgManifestFileMap $manifest 'Candidate build manifest'
    Test-DgAppTree $candidateApp $manifest $manifestHash 'Candidate app'

    $portableZip = Get-DgCanonicalExistingPath ([string]$report.portable_zip) 'Portable ZIP'
    $portableSidecar = Get-DgCanonicalExistingPath ([string]$report.portable_sidecar) 'Portable ZIP sidecar'
    $null = Assert-DgUnderRoot $portableZip $candidateBuildRoot 'Portable ZIP'
    $null = Assert-DgUnderRoot $portableSidecar $candidateBuildRoot 'Portable ZIP sidecar'
    if ((Split-Path -Leaf $portableZip) -ne "GeWuWorkbench-$version-portable.zip" -or
        (Split-Path -Leaf $portableSidecar) -ne ((Split-Path -Leaf $portableZip) + '.sha256')) {
        throw 'Portable ZIP and sidecar names do not match the immutable candidate naming contract.'
    }
    Assert-DgPlainPath $portableZip 'Portable ZIP' | Out-Null
    Assert-DgPlainPath $portableSidecar 'Portable ZIP sidecar' | Out-Null
    $portableHash = Get-DgSha256 $portableZip
    $sidecarHash = Get-DgSha256 $portableSidecar
    if ($portableHash -ne (Assert-DgHash $report.portable_sha256 'portable_sha256') -or
        $sidecarHash -ne (Assert-DgHash $report.portable_sidecar_sha256 'portable_sidecar_sha256')) {
        throw 'Portable ZIP or sidecar SHA-256 changed.'
    }
    $sidecarText = (Get-Content -LiteralPath $portableSidecar -Raw -Encoding UTF8).Trim()
    if ($sidecarText -ne "$portableHash  $(Split-Path -Leaf $portableZip)") {
        throw 'Portable sidecar must contain exactly <sha256><two spaces><zip basename>.'
    }
    Test-DgPortableZip $portableZip $manifest $manifestHash 'Portable ZIP'
    $automated = Test-DgOfflineReport ([string]$report.automated_report) ([string]$report.automated_report_sha256) $reportsRoot $commit

    return [pscustomobject]@{
        profile = 'dev'; report_path = $reportFile; report_sha256 = Get-DgSha256 $reportFile
        version = $version; build_id = [string]$report.build_id; commit = $commit; source_sha256 = [string]$manifest.source_sha256
        candidate_app = $candidateApp; candidate_build_root = $candidateBuildRoot; manifest = $manifest; manifest_path = $manifestPath
        manifest_sha256 = $manifestHash; portable_zip = $portableZip; portable_sha256 = $portableHash
        portable_sidecar = $portableSidecar; portable_sidecar_sha256 = $sidecarHash
        automated_report = [string]$report.automated_report; automated_report_sha256 = [string]$report.automated_report_sha256
        automated = $automated; file_count = $manifestMap.Count
    }
}

function Test-DgManualAcceptance([string]$ResultPath, [string]$ReportsRoot, [string]$BuildsRoot, [string]$ExpectedDevData) {
    $reportsRoot = Get-DgCanonicalPath $ReportsRoot 'Reports root'
    $resultFile = Get-DgCanonicalExistingPath $ResultPath 'Manual acceptance result'
    $null = Assert-DgUnderRoot $resultFile $reportsRoot 'Manual acceptance result'
    Assert-DgPlainPath $resultFile 'Manual acceptance result' | Out-Null
    $result = Read-DgJson $resultFile 'Manual acceptance result'
    if ($result.schema -ne 1 -or $result.status -ne 'passed' -or $result.profile -ne 'dev' -or
        $result.dirty -ne $false) {
        throw 'Manual result must identify a passed DEV acceptance on a clean candidate.'
    }
    if ($result.runtime_policy -notin @('offline', 'live_validation') -or $result.runtime_instance_id -ne 'DEV') {
        throw 'Manual result must bind the DEV runtime policy and instance id.'
    }
    $expectedData = Get-DgCanonicalPath $ExpectedDevData 'Expected DEV data path'
    if ((Get-DgCanonicalPath ([string]$result.data_path) 'Manual result data path') -ine $expectedData) {
        throw 'Manual result does not identify the configured DEV data path.'
    }

    $candidate = Test-DgCandidateBundle ([string]$result.candidate_report) $reportsRoot $BuildsRoot
    if ($candidate.report_sha256 -ne (Assert-DgHash $result.candidate_report_sha256 'candidate_report_sha256') -or
        $candidate.version -ne $result.version -or $candidate.build_id -ne $result.build_id -or
        $candidate.commit -ne ([string]$result.commit).ToLowerInvariant() -or
        $candidate.source_sha256 -ne (Assert-DgHash $result.source_sha256 'source_sha256') -or
        $candidate.candidate_app -ine (Get-DgCanonicalPath ([string]$result.candidate_app) 'Candidate app') -or
        $candidate.manifest_sha256 -ne (Assert-DgHash $result.app_manifest_sha256 'app_manifest_sha256') -or
        $candidate.portable_zip -ine (Get-DgCanonicalPath ([string]$result.portable_zip) 'Portable ZIP') -or
        $candidate.portable_sha256 -ne (Assert-DgHash $result.portable_sha256 'portable_sha256') -or
        $candidate.portable_sidecar -ine (Get-DgCanonicalPath ([string]$result.portable_sidecar) 'Portable ZIP sidecar') -or
        $candidate.portable_sidecar_sha256 -ne (Assert-DgHash $result.portable_sidecar_sha256 'portable_sidecar_sha256')) {
        throw 'Manual result and immutable DEV candidate do not identify the same build and artifacts.'
    }

    $checklistPath = Get-DgCanonicalExistingPath ([string]$result.manual_checklist) 'Manual checklist'
    $null = Assert-DgUnderRoot $checklistPath $reportsRoot 'Manual checklist'
    Assert-DgPlainPath $checklistPath 'Manual checklist' | Out-Null
    if ((Get-DgSha256 $checklistPath) -ne (Assert-DgHash $result.manual_checklist_sha256 'manual_checklist_sha256')) {
        throw 'Manual checklist SHA-256 changed.'
    }
    $items = @($result.checklist_items)
    if ($items.Count -eq 0 -or @($items | Where-Object { $_.passed -ne $true }).Count -gt 0) {
        throw 'A passing DEV manual result requires every checklist item to be checked.'
    }
    $checklistText = Get-Content -LiteralPath $checklistPath -Raw -Encoding UTF8
    foreach ($item in $items) {
        $label = [string]$item.label
        if ([string]::IsNullOrWhiteSpace($label) -or $checklistText -notmatch ('(?m)^- \[x\] ' + [Regex]::Escape($label) + '\s*$')) {
            throw 'Manual result checklist items must each match a human-checked line in the hashed checklist.'
        }
    }
    if ($checklistText -match '(?m)^- \[ \] ') { throw 'Hashed manual checklist still contains unchecked items.' }
    if ([string]::IsNullOrWhiteSpace([string]$result.run_id) -or [string]::IsNullOrWhiteSpace([string]$result.human_tested_at)) {
        throw 'Passing manual result must carry a human acceptance Run ID and timestamp.'
    }
    if ([string]$result.automated_report -ine $candidate.automated_report -or
        (Assert-DgHash $result.automated_report_sha256 'automated_report_sha256') -ne $candidate.automated_report_sha256) {
        throw 'Manual result does not bind the candidate offline automated report.'
    }
    $runtimeRecordPath = Get-DgCanonicalExistingPath ([string]$result.run_center_record) 'Run Center DEV record'
    $null = Assert-DgUnderRoot $runtimeRecordPath $reportsRoot 'Run Center DEV record snapshot'
    Assert-DgPlainPath $runtimeRecordPath 'Run Center DEV record' | Out-Null
    if ((Get-DgSha256 $runtimeRecordPath) -ne (Assert-DgHash $result.run_center_record_sha256 'run_center_record_sha256')) {
        throw 'Run Center DEV process record hash changed after human acceptance.'
    }
    $runtimeRecord = Read-DgJson $runtimeRecordPath 'Run Center DEV record'
    if ($runtimeRecord.profile -ne 'DEV' -or $runtimeRecord.dev_candidate_report -ine $candidate.report_path -or
        $runtimeRecord.dev_candidate_report_sha256 -ne $candidate.report_sha256 -or
        $runtimeRecord.build_id -ne $candidate.build_id -or $runtimeRecord.commit -ne $candidate.commit -or
        (Get-DgCanonicalPath ([string]$runtimeRecord.data) 'Run Center DEV data path') -ine $expectedData) {
        throw 'Run Center DEV process record does not identify the manually accepted candidate and data path.'
    }

    return [pscustomobject]@{
        profile = 'dev'; result_path = $resultFile; result_sha256 = Get-DgSha256 $resultFile
        result = $result; candidate = $candidate; checklist_path = $checklistPath; runtime_record_path = $runtimeRecordPath
    }
}

function Test-DgProdVerification([string]$VerificationPath, [string]$PromotionPath, [string]$RunRecordPath,
    [string]$ExpectedApp, [string]$ExpectedData, [string]$ExpectedCache) {
    $verificationFile = Get-DgCanonicalExistingPath $VerificationPath 'Live PROD verification'
    $promotionFile = Get-DgCanonicalExistingPath $PromotionPath 'PROD promotion record'
    $runRecordFile = Get-DgCanonicalExistingPath $RunRecordPath 'Run Center PROD record'
    $appPath = Get-DgCanonicalExistingPath $ExpectedApp 'Promoted PROD app'
    foreach ($item in @(@($verificationFile,'PROD verification'),@($promotionFile,'PROD promotion'),@($runRecordFile,'PROD process record'))) {
        Assert-DgPlainPath $item[0] $item[1] | Out-Null
    }
    $promotionRoot = Split-Path -Parent $promotionFile
    $null = Assert-DgUnderRoot $verificationFile $promotionRoot 'PROD verification'
    if ((Get-DgCanonicalPath $appPath 'Promoted PROD app') -ine (Join-Path $promotionRoot 'app')) {
        throw 'PROD verification is not adjacent to the configured immutable app and promotion record.'
    }
    $promotion = Read-DgJson $promotionFile 'PROD promotion record'
    $verification = Read-DgJson $verificationFile 'Live PROD verification'
    $record = Read-DgJson $runRecordFile 'Run Center PROD record'
    $promotionHash = Get-DgSha256 $promotionFile
    $verificationHash = Get-DgSha256 $verificationFile
    $manifestPath = Join-Path $appPath 'build-manifest.json'
    $manifest = Read-DgJson $manifestPath 'Promoted PROD manifest'
    $manifestHash = Get-DgSha256 $manifestPath
    $expectedDataPath = Get-DgCanonicalPath $ExpectedData 'Expected PROD data path'
    $expectedCachePath = Get-DgCanonicalPath $ExpectedCache 'Expected PROD cache path'
    if ($promotion.status -ne 'promoted_pending_prod_verification' -or
        $verification.schema -ne 1 -or $verification.status -ne 'verified_live_prod' -or
        $verification.profile -ne 'prod' -or $verification.instance_id -ne 'PROD' -or
        $verification.version -ne $promotion.version -or $verification.build_id -ne $promotion.build_id -or
        $verification.commit -ne $promotion.commit -or $verification.source_sha256 -ne $promotion.source_sha256 -or
        $verification.manifest_sha256 -ne $manifestHash -or $verification.app_manifest_sha256 -ne $manifestHash -or
        $manifest.version -ne $promotion.version -or $manifest.build_id -ne $promotion.build_id -or
        $manifest.commit -ne $promotion.commit -or $manifest.source_sha256 -ne $promotion.source_sha256 -or
        [IO.Path]::GetFullPath([string]$verification.app) -ine $appPath -or
        [IO.Path]::GetFullPath([string]$promotion.target_app) -ine $appPath -or
        [IO.Path]::GetFullPath([string]$verification.promotion_path) -ine $promotionFile -or
        [string]$verification.promotion_sha256 -ne $promotionHash -or
        [IO.Path]::GetFullPath([string]$verification.run_record) -ine $runRecordFile -or
        [IO.Path]::GetFullPath([string]$verification.data_path) -ine $expectedDataPath -or
        [IO.Path]::GetFullPath([string]$verification.cache_path) -ine $expectedCachePath -or
        $record.profile -ne 'PROD' -or $record.build_id -ne $promotion.build_id -or $record.commit -ne $promotion.commit -or
        [IO.Path]::GetFullPath([string]$record.data) -ine $expectedDataPath -or
        [IO.Path]::GetFullPath([string]$record.cache) -ine $expectedCachePath -or
        [IO.Path]::GetFullPath([string]$record.prod_verification_path) -ine $verificationFile -or
        [string]$record.prod_verification_sha256 -ne $verificationHash) {
        throw 'Saved PROD verification, promotion, manifest, and Run Center record do not bind the same production identity.'
    }
    $verifiedProcess = $verification.process
    $recordedProcess = @($record.processes | Where-Object role -eq 'app-runtime') | Select-Object -First 1
    if (-not $recordedProcess -or [int]$verifiedProcess.pid -le 0 -or
        [int]$recordedProcess.pid -ne [int]$verifiedProcess.pid -or
        [IO.Path]::GetFullPath([string]$recordedProcess.executable) -ine [IO.Path]::GetFullPath([string]$verifiedProcess.executable) -or
        [string]$recordedProcess.identity_token -ne [string]$verifiedProcess.identity_token) {
        throw 'PROD verification does not bind the exact Run Center embedded runtime process.'
    }
    return [pscustomobject]@{
        verification = $verification; verification_path = $verificationFile; verification_sha256 = $verificationHash
        promotion = $promotion; promotion_path = $promotionFile; promotion_sha256 = $promotionHash
        record = $record; record_path = $runRecordFile; app = $appPath; manifest_sha256 = $manifestHash
    }
}

function Test-DgProdBackupReadiness([string]$ReportPath, [string]$BackupsRoot, [string]$ExpectedData, [string]$ExpectedCache) {
    $root = Get-DgCanonicalPath $BackupsRoot 'PROD backup root'
    $reportFile = Get-DgCanonicalExistingPath $ReportPath 'PROD backup readiness report'
    $null = Assert-DgUnderRoot $reportFile $root 'PROD backup readiness report'
    Assert-DgPlainPath $reportFile 'PROD backup readiness report' | Out-Null
    $report = Read-DgJson $reportFile 'PROD backup readiness report'
    $dataPath = Get-DgCanonicalPath $ExpectedData 'Expected PROD data path'
    $cachePath = Get-DgCanonicalPath $ExpectedCache 'Expected PROD cache path'
    if ($report.schema -ne 1 -or $report.status -ne 'passed' -or $report.profile -ne 'prod' -or
        (Get-DgCanonicalPath ([string]$report.source_data_path) 'Backup source data') -ine $dataPath -or
        (Get-DgCanonicalPath ([string]$report.source_cache_path) 'Backup source cache') -ine $cachePath -or
        [string]::IsNullOrWhiteSpace([string]$report.run_id)) {
        throw 'Backup readiness report must identify a passed backup of the fixed PROD data and cache paths.'
    }
    $artifactKinds = @{}
    foreach ($artifact in @($report.artifacts)) {
        $kind = [string]$artifact.kind
        if ($kind -notin @('database', 'reasonix_sessions') -or $artifactKinds.ContainsKey($kind)) {
            throw 'Backup readiness report must contain one unique database and Reasonix sessions artifact.'
        }
        $artifactPath = Get-DgCanonicalExistingPath ([string]$artifact.path) "PROD $kind backup"
        $null = Assert-DgUnderRoot $artifactPath $root "PROD $kind backup"
        Assert-DgPlainPath $artifactPath "PROD $kind backup" | Out-Null
        if ((Get-DgSha256 $artifactPath) -ne (Assert-DgHash $artifact.sha256 "$kind backup sha256")) {
            throw "PROD $kind backup hash changed."
        }
        $artifactKinds[$kind] = $artifactPath
    }
    if ($artifactKinds.Count -ne 2) { throw 'Both database and Reasonix session backups are required.' }
    $restore = $report.restore_validation
    if ($restore.status -ne 'passed') { throw 'Backup readiness report has no passing isolated restore validation.' }
    $restoreRoot = Get-DgCanonicalExistingPath ([string]$restore.restore_root) 'Isolated restore root'
    $null = Assert-DgUnderRoot $restoreRoot $root 'Isolated restore root'
    if ($restoreRoot -ieq $dataPath -or $restoreRoot -ieq $cachePath -or
        $restoreRoot.StartsWith($dataPath + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $restoreRoot.StartsWith($cachePath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Restore validation must use a disposable isolated copy, never PROD data or cache.'
    }
    $restoredDb = Get-DgCanonicalExistingPath ([string]$restore.restored_database_path) 'Restored database copy'
    $null = Assert-DgUnderRoot $restoredDb $restoreRoot 'Restored database copy'
    Assert-DgPlainPath $restoredDb 'Restored database copy' | Out-Null
    if ((Get-DgSha256 $restoredDb) -ne (Assert-DgHash $restore.restored_database_sha256 'restored_database_sha256')) {
        throw 'Restored database copy hash changed.'
    }
    $originalIds = @($restore.original_reasonix_session_ids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    $restoredIds = @($restore.restored_reasonix_session_ids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    $sessionIdDifferences = @(Compare-Object $originalIds $restoredIds)
    if ($originalIds.Count -eq 0 -or $restoredIds.Count -ne $originalIds.Count -or
        $sessionIdDifferences.Count -gt 0) {
        throw 'Isolated restore did not preserve every pre-upgrade Reasonix session ID.'
    }
    if (-not $restore.reasonix_restore_status -or $restore.reasonix_restore_status -ne 'passed') {
        throw 'Reasonix session restore was not explicitly verified as passed.'
    }
    return [pscustomobject]@{
        report = $report; report_path = $reportFile; report_sha256 = Get-DgSha256 $reportFile
        database_backup = $artifactKinds['database']; reasonix_backup = $artifactKinds['reasonix_sessions']
        restore_root = $restoreRoot; restored_database = $restoredDb; session_ids = $originalIds
    }
}

Export-ModuleMember -Function Get-DgSha256, Assert-DgHash, Get-DgCanonicalExistingPath, Get-DgCanonicalPath,
    Assert-DgUnderRoot, Assert-DgPlainPath, Assert-DgNoReparseAncestors, Read-DgJson, Test-DgAppTree, Test-DgPortableZip, Expand-DgPortableZip,
    Test-DgOfflineReport, Test-DgCandidateBundle, Test-DgManualAcceptance, Test-DgProdVerification, Test-DgProdBackupReadiness
