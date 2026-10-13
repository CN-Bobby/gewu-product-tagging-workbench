[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ManualResult
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter')).TrimEnd('\')
$configPath = Join-Path $runCenterRoot 'config.json'
$lockPath = Join-Path $runCenterRoot 'maintenance.lock'
$reportsRoot = [IO.Path]::GetFullPath('K:\GeWuTest\Reports').TrimEnd('\')
$testApp = [IO.Path]::GetFullPath('K:\GeWuTest\Current\app').TrimEnd('\')
$prodRoot = [IO.Path]::GetFullPath('K:\Bobbytagging').TrimEnd('\')
$prodData = [IO.Path]::GetFullPath('K:\Bobbytagging\Data').TrimEnd('\')
$prodCache = [IO.Path]::GetFullPath('K:\Bobbytagging\Cache').TrimEnd('\')
$stableApp = [IO.Path]::GetFullPath('K:\Bobbytagging\app').TrimEnd('\')
$protectedBeta8 = [IO.Path]::GetFullPath('K:\Bobbytagging\Candidate-beta.8').TrimEnd('\')

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Hash([object]$Value, [string]$Label) {
    $text = [string]$Value
    if ($text -notmatch '^[0-9a-fA-F]{64}$') { throw "$Label must be a 64-character SHA-256." }
    return $text.ToLowerInvariant()
}

function Get-CanonicalExistingPath([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw "$Label must be an absolute path."
    }
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    return [IO.Path]::GetFullPath($resolved.ProviderPath).TrimEnd('\')
}

function Assert-UnderRoot([string]$Path, [string]$Root, [string]$Label) {
    $canonicalPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $canonicalRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($canonicalPath -ne $canonicalRoot -and
        -not $canonicalPath.StartsWith($canonicalRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside its approved root: $canonicalPath"
    }
    return $canonicalPath
}

function Assert-UnderOneOf([string]$Path, [string[]]$Roots, [string]$Label) {
    foreach ($root in $Roots) {
        try { return (Assert-UnderRoot $Path $root $Label) } catch { }
    }
    throw "$Label is outside Reports and the configured candidate build directory: $Path"
}

function Assert-PlainDirectory([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must be a normal directory, not a reparse point: $Path"
    }
}

function Assert-PlainFile([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must be a normal file, not a reparse point: $Path"
    }
}

function Get-ManifestRelativePath([string]$Relative) {
    if ([string]::IsNullOrWhiteSpace($Relative) -or $Relative.Contains('\') -or
        [IO.Path]::IsPathRooted($Relative) -or $Relative -match '[:<>"|?*]' -or
        $Relative.StartsWith('/') -or $Relative.EndsWith('/')) {
        throw "Unsafe manifest/ZIP path: $Relative"
    }
    $parts = $Relative.Split('/')
    foreach ($part in $parts) {
        if ($part -in @('', '.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') {
            throw "Unsafe manifest/ZIP path: $Relative"
        }
    }
    return ($parts -join '\')
}

function Get-ManifestFileMap($Manifest, [string]$Label) {
    if (-not $Manifest.files) { throw "$Label has no file inventory." }
    $map = @{}
    foreach ($property in $Manifest.files.PSObject.Properties) {
        $relative = [string]$property.Name
        $null = Get-ManifestRelativePath $relative
        if ($relative -ieq 'build-manifest.json') { throw "$Label must not list its own manifest in files." }
        $hash = Assert-Hash $property.Value "$Label files[$relative]"
        if ($map.ContainsKey($relative)) { throw "$Label has a duplicate file path: $relative" }
        $map[$relative] = $hash
    }
    if ($map.Count -eq 0) { throw "$Label file inventory is empty." }
    return ,$map
}

function Test-AppTree([string]$AppPath, $Manifest, [string]$ExpectedManifestHash, [string]$Label) {
    Assert-PlainDirectory $AppPath $Label
    $manifestPath = Join-Path $AppPath 'build-manifest.json'
    Assert-PlainFile $manifestPath "$Label build manifest"
    if ((Get-Sha256 $manifestPath) -ne $ExpectedManifestHash) { throw "$Label build manifest hash changed." }

    $map = Get-ManifestFileMap $Manifest $Label
    $actual = @{}
    foreach ($directory in Get-ChildItem -LiteralPath $AppPath -Recurse -Directory -Force) {
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label contains a reparse-point directory: $($directory.FullName)" }
    }
    foreach ($file in Get-ChildItem -LiteralPath $AppPath -Recurse -File -Force) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label contains a reparse-point file: $($file.FullName)" }
        $relative = $file.FullName.Substring($AppPath.Length).TrimStart('\').Replace('\', '/')
        if ($relative -ieq 'build-manifest.json') {
            if ($relative -cne 'build-manifest.json') { throw "$Label manifest file name is not exact." }
            continue
        }
        $null = Get-ManifestRelativePath $relative
        if ($actual.ContainsKey($relative)) { throw "$Label contains a duplicate file path: $relative" }
        $actual[$relative] = $file.FullName
    }
    if ($actual.Count -ne $map.Count) { throw "$Label file count does not match build-manifest.json." }
    foreach ($relative in $map.Keys) {
        if (-not $actual.ContainsKey($relative)) { throw "$Label is missing manifest file: $relative" }
        $actualRelative = @($actual.Keys | Where-Object { $_ -ieq $relative })[0]
        if ($actualRelative -cne $relative) { throw "$Label file path casing differs from manifest: $relative" }
        if ((Get-Sha256 $actual[$relative]) -ne $map[$relative]) { throw "$Label file hash does not match manifest: $relative" }
    }
}

function Get-ZipEntryHash($Entry) {
    $stream = $Entry.Open()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Expand-VerifiedZip([string]$ZipPath, [string]$StageRoot, [string]$StageApp, $Manifest, [string]$ExpectedManifestHash) {
    Add-Type -AssemblyName System.IO.Compression
    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    $entries = @{}
    $seen = @{}
    $map = Get-ManifestFileMap $Manifest 'Portable ZIP manifest'
    try {
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('\') -or $name.StartsWith('/') -or
                [IO.Path]::IsPathRooted($name) -or $name -match '(^|/)\.\.(/|$)|(^|/)\.(?:/|$)|:' ) {
                throw "Portable ZIP contains an unsafe path: $name"
            }
            $isDirectory = $name.EndsWith('/')
            $relative = if ($isDirectory) { $name.TrimEnd('/') } else { $name }
            if (-not $relative) { throw 'Portable ZIP contains an empty root entry.' }
            $null = Get-ManifestRelativePath $relative
            $key = $relative.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { throw "Portable ZIP contains a duplicate path: $relative" }
            $seen[$key] = $true
            $unixMode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($unixMode -eq 0xA000) { throw "Portable ZIP contains a symbolic link: $relative" }
            if ($isDirectory) { continue }
            if ($relative -ieq 'build-manifest.json' -and $relative -cne 'build-manifest.json') {
                throw 'Portable ZIP manifest must be named exactly build-manifest.json at the ZIP root.'
            }
            $entries[$relative] = $entry
        }

        $manifestEntry = $entries['build-manifest.json']
        if (-not $manifestEntry) { throw 'Portable ZIP is missing root build-manifest.json.' }
        if ((Get-ZipEntryHash $manifestEntry) -ne $ExpectedManifestHash) {
            throw 'Portable ZIP build-manifest.json hash does not match the accepted TEST manifest.'
        }
        if ($entries.Count -ne ($map.Count + 1)) { throw 'Portable ZIP contains files outside the build manifest or is incomplete.' }
        foreach ($relative in $map.Keys) {
            $entry = $entries[$relative]
            if (-not $entry) { throw "Portable ZIP is missing manifest file: $relative" }
            if ([string]$entry.FullName -cne $relative) { throw "Portable ZIP path casing differs from manifest: $relative" }
            if ((Get-ZipEntryHash $entry) -ne $map[$relative]) { throw "Portable ZIP file hash does not match manifest: $relative" }
        }

        New-Item -ItemType Directory -Path $StageRoot -ErrorAction Stop | Out-Null
        New-Item -ItemType Directory -Path $StageApp -ErrorAction Stop | Out-Null
        foreach ($relative in $entries.Keys) {
            $entry = $entries[$relative]
            $relativeWindows = Get-ManifestRelativePath $relative
            $destination = Join-Path $StageApp $relativeWindows
            $null = Assert-UnderRoot $destination $StageApp 'Extracted file'
            $parent = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
                New-Item -ItemType Directory -Force -Path $parent | Out-Null
            }
            $input = $entry.Open()
            try {
                $output = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $input.CopyTo($output) } finally { $output.Dispose() }
            } finally { $input.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Assert-ReportPath([string]$Path, [string]$Root, [string]$Label) {
    $canonical = Get-CanonicalExistingPath $Path $Label
    $null = Assert-UnderRoot $canonical $Root $Label
    Assert-PlainFile $canonical $Label
    return $canonical
}

function Write-NewJson([string]$Path, $Value) {
    $json = ConvertTo-Json -InputObject $Value -Depth 100
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = $encoding.GetBytes($json + "`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function Update-ProdAppOnly([string]$Path, [string]$NewApp, $OriginalConfig) {
    $rawBytes = [IO.File]::ReadAllBytes($Path)
    $hasBom = $rawBytes.Length -ge 3 -and $rawBytes[0] -eq 0xEF -and $rawBytes[1] -eq 0xBB -and $rawBytes[2] -eq 0xBF
    $offset = if ($hasBom) { 3 } else { 0 }
    $text = [Text.Encoding]::UTF8.GetString($rawBytes, $offset, $rawBytes.Length - $offset)
    $profilePattern = '(?s)"PROD"\s*:\s*\{(?<body>[^{}]*)\}'
    $profileMatches = [regex]::Matches($text, $profilePattern)
    if ($profileMatches.Count -ne 1) { throw 'Could not uniquely locate config.profiles.PROD.' }
    $profileMatch = $profileMatches[0]
    $body = $profileMatch.Groups['body'].Value
    $appMatches = [regex]::Matches($body, '(?m)(?<prefix>^[\t ]*"app"[\t ]*:[\t ]*")[^"]*(?<suffix>")')
    if ($appMatches.Count -ne 1) { throw 'Could not uniquely locate config.profiles.PROD.app.' }
    $appMatch = $appMatches[0]
    $quotedPath = ConvertTo-Json -InputObject $NewApp -Compress
    $replacement = $appMatch.Groups['prefix'].Value + $quotedPath.Trim('"') + $appMatch.Groups['suffix'].Value
    $newBody = $body.Substring(0, $appMatch.Index) + $replacement + $body.Substring($appMatch.Index + $appMatch.Length)
    $newText = $text.Substring(0, $profileMatch.Groups['body'].Index) + $newBody +
        $text.Substring($profileMatch.Groups['body'].Index + $profileMatch.Groups['body'].Length)

    $expected = $OriginalConfig | ConvertTo-Json -Depth 100 | ConvertFrom-Json
    $expected.profiles.PROD.app = $NewApp
    $parsed = $newText | ConvertFrom-Json
    if ((ConvertTo-Json -InputObject $parsed -Depth 100 -Compress) -ne
        (ConvertTo-Json -InputObject $expected -Depth 100 -Compress)) {
        throw 'Refusing to rewrite Run Center config because another value would change.'
    }

    $encoding = [System.Text.UTF8Encoding]::new($false)
    $newContent = $encoding.GetBytes($newText)
    if ($hasBom) { $newContent = [byte[]](@(0xEF, 0xBB, 0xBF) + $newContent) }
    $tempPath = $Path + '.promote-' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllBytes($tempPath, $newContent)
    try {
        [IO.File]::Replace($tempPath, $Path, $null)
    } catch {
        throw "Atomic PROD app configuration update failed; the original config remains authoritative: $($_.Exception.Message)"
    } finally {
        if (Test-Path -LiteralPath $tempPath -PathType Leaf) { Remove-Item -LiteralPath $tempPath -Force }
    }

    $saved = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([IO.Path]::GetFullPath([string]$saved.profiles.PROD.app).TrimEnd('\') -ine $NewApp) {
        throw 'Run Center config did not retain the promoted PROD app path.'
    }
    if ([IO.Path]::GetFullPath([string]$saved.profiles.PROD.data).TrimEnd('\') -ine $prodData -or
        [IO.Path]::GetFullPath([string]$saved.profiles.PROD.cache).TrimEnd('\') -ine $prodCache) {
        throw 'PROD data/cache config changed unexpectedly; no data/cache files were accessed.'
    }
}

$maintenanceLock = $null
$stageRoot = $null
try {
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "Run Center config is missing: $configPath" }
    Assert-PlainFile $configPath 'Run Center config'
    try {
        $maintenanceLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw "Run Center maintenance lock is busy; promotion stopped: $lockPath"
    }

    $configText = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
    $config = $configText | ConvertFrom-Json
    if (-not $config.profiles.TEST -or -not $config.profiles.PROD) { throw 'Run Center TEST/PROD profiles are missing.' }
    if ([IO.Path]::GetFullPath([string]$config.profiles.TEST.reports).TrimEnd('\') -ine $reportsRoot) {
        throw 'Configured TEST reports path must be K:\GeWuTest\Reports.'
    }
    if ([IO.Path]::GetFullPath([string]$config.profiles.TEST.app).TrimEnd('\') -ine $testApp) {
        throw 'Configured TEST app must be K:\GeWuTest\Current\app.'
    }
    if ([IO.Path]::GetFullPath([string]$config.profiles.PROD.data).TrimEnd('\') -ine $prodData -or
        [IO.Path]::GetFullPath([string]$config.profiles.PROD.cache).TrimEnd('\') -ine $prodCache) {
        throw 'PROD data/cache configuration is not fixed to K:\Bobbytagging\Data and Cache.'
    }
    $configuredCurrentApp = [IO.Path]::GetFullPath([string]$config.profiles.PROD.app).TrimEnd('\')
    if ($configuredCurrentApp -ine $stableApp -and $configuredCurrentApp -notmatch '^K:\\Bobbytagging\\Candidate-beta\.[1-9][0-9]*\\app$') {
        throw "Current PROD app path is outside the known immutable app locations: $configuredCurrentApp"
    }

    if (-not (Test-Path -LiteralPath $reportsRoot -PathType Container)) { throw "TEST Reports directory is missing: $reportsRoot" }
    $manualPath = Assert-ReportPath $ManualResult $reportsRoot 'Manual result'
    $manualHash = Get-Sha256 $manualPath
    $manual = Get-Content -LiteralPath $manualPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manual.schema -ne 2 -or $manual.status -ne 'passed' -or $manual.profile -ne 'test') {
        throw 'Manual result must be schema 2, status passed, and profile test.'
    }
    foreach ($field in @('version','build_id','commit','candidate_app','app_manifest_sha256','portable_zip','portable_sha256',
            'portable_sidecar','portable_sidecar_sha256','candidate_report','candidate_report_sha256','manual_checklist','manual_checklist_sha256')) {
        if ([string]::IsNullOrWhiteSpace([string]$manual.$field)) { throw "Manual result is missing required field: $field" }
    }
    if ([string]$manual.version -notmatch '^0\.5\.0-beta\.(?<number>[1-9][0-9]*)$') {
        throw "Unsupported candidate version: $($manual.version)"
    }
    $version = [string]$manual.version
    $versionNumber = $Matches.number
    $commit = ([string]$manual.commit).ToLowerInvariant()
    if ($commit -notmatch '^[0-9a-f]{40}$') { throw 'Manual result commit must be a full 40-character Git commit id.' }
    if ([string]$manual.build_id -ne ($version + '+' + $commit.Substring(0,12))) {
        throw 'Manual result build_id does not match its exact version and commit.'
    }
    if ($manual.dirty -ne $false) { throw 'Manual result must bind a clean, frozen candidate.' }
    $manifestHash = Assert-Hash $manual.app_manifest_sha256 'app_manifest_sha256'
    $portableHash = Assert-Hash $manual.portable_sha256 'portable_sha256'
    $sidecarHash = Assert-Hash $manual.portable_sidecar_sha256 'portable_sidecar_sha256'
    $candidateReportHash = Assert-Hash $manual.candidate_report_sha256 'candidate_report_sha256'
    $checklistHash = Assert-Hash $manual.manual_checklist_sha256 'manual_checklist_sha256'

    $candidateReportPath = Assert-ReportPath ([string]$manual.candidate_report) $reportsRoot 'Candidate report'
    if ((Get-Sha256 $candidateReportPath) -ne $candidateReportHash) { throw 'Candidate report SHA-256 changed.' }
    $candidateReport = Get-Content -LiteralPath $candidateReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($candidateReport.schema -ne 1 -or $candidateReport.status -notin @('activated_pending_acceptance','running_pending_manual_acceptance') -or
        $candidateReport.profile -ne 'test' -or $candidateReport.dirty -ne $false -or
        [string]$candidateReport.version -ne $version -or [string]$candidateReport.build_id -ne [string]$manual.build_id -or
        ([string]$candidateReport.commit).ToLowerInvariant() -ne $commit) {
        throw 'Candidate report is not the matching clean TEST candidate awaiting or completing manual acceptance.'
    }

    $checklistPath = Assert-ReportPath ([string]$manual.manual_checklist) $reportsRoot 'Manual checklist'
    if ((Get-Sha256 $checklistPath) -ne $checklistHash) { throw 'Manual checklist SHA-256 changed.' }
    if (-not $manual.automated_report -or -not $manual.automated_report_sha256) { throw 'Manual result does not bind an automated TEST report.' }
    $automatedPath = Assert-ReportPath ([string]$manual.automated_report) $reportsRoot 'Automated report'
    $automatedHash = Assert-Hash $manual.automated_report_sha256 'automated_report_sha256'
    if ((Get-Sha256 $automatedPath) -ne $automatedHash) { throw 'Automated TEST report SHA-256 changed.' }
    if ($candidateReport.automated_report -and $candidateReport.automated_report_sha256) {
        if ([IO.Path]::GetFullPath([string]$candidateReport.automated_report).TrimEnd('\') -ine $automatedPath -or
            ([string]$candidateReport.automated_report_sha256).ToLowerInvariant() -ne $automatedHash) {
            throw 'Manual and candidate reports do not bind the same automated report.'
        }
    }

    $candidateApp = Get-CanonicalExistingPath ([string]$manual.candidate_app) 'candidate_app'
    if ($candidateApp -ine $testApp) { throw 'candidate_app must be the configured TEST Current\app directory.' }
    Assert-PlainDirectory $candidateApp 'TEST candidate app'
    $candidateBuildApp = Get-CanonicalExistingPath ([string]$candidateReport.candidate_app) 'candidate build app'
    if ((Split-Path -Leaf $candidateBuildApp) -ine 'app') { throw 'Candidate build app must be a named app directory.' }
    $candidateProjectRoot = [IO.Path]::GetFullPath([string]$config.candidateProjectRoot).TrimEnd('\')
    if (-not [IO.Path]::IsPathRooted($candidateProjectRoot)) { throw 'Run Center candidateProjectRoot must be absolute.' }
    $buildsRoot = Join-Path $candidateProjectRoot 'builds'
    $candidateBuildRoot = [IO.Path]::GetFullPath((Split-Path -Parent $candidateBuildApp)).TrimEnd('\')
    $null = Assert-UnderRoot $candidateBuildRoot $buildsRoot 'Candidate build directory'
    $expectedBuildLeaf = $version + '-candidate.' + $commit.Substring(0, 12)
    if ((Split-Path -Leaf $candidateBuildRoot) -ine $expectedBuildLeaf) {
        throw 'Candidate build directory name does not match the accepted version and commit prefix.'
    }
    if ([IO.Path]::GetFullPath((Split-Path -Parent $candidateBuildRoot)).TrimEnd('\') -ine [IO.Path]::GetFullPath($buildsRoot).TrimEnd('\')) {
        throw 'Candidate build must be a direct child of the configured candidateProjectRoot\builds directory.'
    }
    if (-not $candidateReport.build_manifest_sha256 -or ([string]$candidateReport.build_manifest_sha256).ToLowerInvariant() -ne $manifestHash) {
        throw 'Candidate report build manifest hash differs from the manually accepted app manifest.'
    }
    if ($candidateReport.manifest_sha256 -and ([string]$candidateReport.manifest_sha256).ToLowerInvariant() -ne $manifestHash) {
        throw 'Candidate report manifest hash differs from the manually accepted app manifest.'
    }

    $candidateBuildManifestPath = Join-Path $candidateBuildApp 'build-manifest.json'
    Assert-PlainFile $candidateBuildManifestPath 'Candidate build manifest'
    $testManifestPath = Join-Path $candidateApp 'build-manifest.json'
    Assert-PlainFile $testManifestPath 'TEST build manifest'
    if ((Get-Sha256 $candidateBuildManifestPath) -ne $manifestHash -or (Get-Sha256 $testManifestPath) -ne $manifestHash) {
        throw 'Candidate build, TEST Current, and manual result do not have the same build-manifest.json SHA-256.'
    }
    $manifest = Get-Content -LiteralPath $testManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$manifest.version -ne $version -or [string]$manifest.build_id -ne [string]$manual.build_id -or
        ([string]$manifest.commit).ToLowerInvariant() -ne $commit -or $manifest.dirty -ne $false) {
        throw 'TEST app manifest does not match the passing manual result identity.'
    }
    Test-AppTree $candidateApp $manifest $manifestHash 'TEST Current app'
    Test-AppTree $candidateBuildApp $manifest $manifestHash 'Immutable candidate build app'

    $portablePath = Get-CanonicalExistingPath ([string]$manual.portable_zip) 'portable_zip'
    $portableSidecar = Get-CanonicalExistingPath ([string]$manual.portable_sidecar) 'portable_sidecar'
    $null = Assert-UnderOneOf $portablePath @($reportsRoot, $candidateBuildRoot) 'Portable ZIP'
    $null = Assert-UnderOneOf $portableSidecar @($reportsRoot, $candidateBuildRoot) 'Portable ZIP sidecar'
    if ((Split-Path -Leaf $portablePath) -ne "GeWuWorkbench-$version-portable.zip" -or
        (Split-Path -Leaf $portableSidecar) -ne ((Split-Path -Leaf $portablePath) + '.sha256')) {
        throw 'Portable ZIP and sidecar names do not match the frozen candidate convention.'
    }
    if ([IO.Path]::GetFullPath([string]$candidateReport.portable_zip).TrimEnd('\') -ine $portablePath -or
        [IO.Path]::GetFullPath([string]$candidateReport.portable_sidecar).TrimEnd('\') -ine $portableSidecar -or
        ([string]$candidateReport.portable_sha256).ToLowerInvariant() -ne $portableHash -or
        ([string]$candidateReport.portable_sidecar_sha256).ToLowerInvariant() -ne $sidecarHash) {
        throw 'Portable artifact paths differ from the immutable candidate report.'
    }
    Assert-PlainFile $portablePath 'Portable ZIP'
    Assert-PlainFile $portableSidecar 'Portable sidecar'
    if ((Get-Sha256 $portablePath) -ne $portableHash) { throw 'Portable ZIP SHA-256 changed.' }
    if ((Get-Sha256 $portableSidecar) -ne $sidecarHash) { throw 'Portable ZIP sidecar SHA-256 changed.' }
    $sidecarLine = (Get-Content -LiteralPath $portableSidecar -Raw -Encoding UTF8).Trim()
    if ($sidecarLine -ne "$portableHash  $(Split-Path -Leaf $portablePath)") {
        throw 'Portable ZIP sidecar must contain exactly <sha256><two spaces><zip basename>.'
    }

    $targetName = "Candidate-beta.$versionNumber"
    $targetRoot = Join-Path $prodRoot $targetName
    $targetApp = Join-Path $targetRoot 'app'
    $promotionPath = Join-Path $targetRoot 'PROMOTION.json'
    if ($targetRoot -ieq $protectedBeta8 -or $targetApp -ieq $stableApp -or
        $targetApp.StartsWith($stableApp + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Computed target overlaps a protected existing PROD app.'
    }
    if (-not (Test-Path -LiteralPath $prodRoot -PathType Container)) { throw 'K:\Bobbytagging root is missing.' }
    Assert-PlainDirectory $prodRoot 'PROD root'
    if (Test-Path -LiteralPath $targetRoot) { throw "Promotion target already exists; refusing to overwrite: $targetRoot" }
    if (Test-Path -LiteralPath $targetApp) { throw "Promotion app target already exists; refusing to overwrite: $targetApp" }
    if (Test-Path -LiteralPath $protectedBeta8 -PathType Container) { Assert-PlainDirectory $protectedBeta8 'Protected beta.8 root' }
    if (Test-Path -LiteralPath $stableApp -PathType Container) { Assert-PlainDirectory $stableApp 'Protected stable app' }

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try { $listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop) }
        catch { throw "Unable to verify that PROD port 8765 is free: $($_.Exception.Message)" }
    } else {
        $netstat = @(& netstat.exe -ano -p TCP 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'Unable to verify that PROD port 8765 is free.' }
        $listeners = @($netstat | Where-Object { $_ -match ':8765\s+.*LISTENING' })
    }
    if ($listeners.Count -gt 0) { throw 'PROD port 8765 has a listener; no app or config was changed.' }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $guid = [Guid]::NewGuid().ToString('N')
    $stageRoot = Join-Path $prodRoot ('.' + $targetName + '.stage-' + $guid)
    $stageApp = Join-Path $stageRoot 'app'
    if (Test-Path -LiteralPath $stageRoot) { throw 'Unique staging directory unexpectedly exists.' }
    Expand-VerifiedZip $portablePath $stageRoot $stageApp $manifest $manifestHash
    Test-AppTree $stageApp $manifest $manifestHash 'Extracted portable app'

    $promotion = [ordered]@{
        schema = 1
        status = 'candidate_verified'
        created_at = [DateTime]::UtcNow.ToString('o')
        version = $version
        build_id = [string]$manual.build_id
        commit = $commit
        manual_result = $manualPath
        manual_result_sha256 = $manualHash
        candidate_report = $candidateReportPath
        candidate_report_sha256 = $candidateReportHash
        manual_checklist_sha256 = $checklistHash
        candidate_app = $candidateApp
        portable_zip = $portablePath
        portable_sha256 = $portableHash
        portable_sidecar = $portableSidecar
        portable_sidecar_sha256 = $sidecarHash
        app_manifest_sha256 = $manifestHash
        target_app = $targetApp
        run_center_config = $configPath
        prod_data_path = $prodData
        prod_cache_path = $prodCache
    }
    Write-NewJson (Join-Path $stageRoot 'PROMOTION.json') $promotion
    [IO.Directory]::Move($stageRoot, $targetRoot)
    $stageRoot = $null

    $promotionPath = Join-Path $targetRoot 'PROMOTION.json'
    $promotion.status = 'config_update_pending'
    Write-NewJson ($promotionPath + '.tmp') $promotion
    [IO.File]::Replace(($promotionPath + '.tmp'), $promotionPath, $null)

    $promotion.status = 'promoted'
    $promotion.config_updated_at = [DateTime]::UtcNow.ToString('o')
    Update-ProdAppOnly $configPath $targetApp $config
    Write-NewJson ($promotionPath + '.tmp') $promotion
    [IO.File]::Replace(($promotionPath + '.tmp'), $promotionPath, $null)

    Write-Output "Portable candidate promoted: $targetApp"
    Write-Output "Build: $($manual.build_id)"
    Write-Output "Manual result: $manualPath (SHA-256 $manualHash)"
    Write-Output "Promotion record: $promotionPath"
} finally {
    if ($maintenanceLock) { $maintenanceLock.Dispose() }
}
