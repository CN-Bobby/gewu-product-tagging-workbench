[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('DEV', 'TEST', 'PROD')][string]$Profile,
    [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
    [ValidateSet('online', 'offline')][string]$ExternalEffectsMode = 'online',
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
    [string]$DevCandidateReport = '',
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$runCenterRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter'))
$expectedConfigPath = Join-Path $runCenterRoot 'config.json'
if ([IO.Path]::GetFullPath($ConfigPath) -ine $expectedConfigPath) {
    throw "Run Center config is pinned to $expectedConfigPath so all actions use the same process identity and maintenance lock."
}

function Get-CanonicalPath([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Test-PathOverlap([string]$Left, [string]$Right) {
    $a=Get-CanonicalPath $Left; $b=Get-CanonicalPath $Right
    return $a.Equals($b,[StringComparison]::OrdinalIgnoreCase) -or
        $a.StartsWith($b+'\',[StringComparison]::OrdinalIgnoreCase) -or
        $b.StartsWith($a+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Get-PortOwner([int]$Port) {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
}
function Get-CreationUtcTicks($ProcessInfo) {
    if (-not $ProcessInfo.CreationDate) { return $null }
    try { return [Management.ManagementDateTimeConverter]::ToDateTime($ProcessInfo.CreationDate).ToUniversalTime().Ticks }
    catch { return $null }
}
function Get-ProcessIdentity([int]$Id) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue
}
function Get-VenvBasePython([string]$VenvPython) {
    $venvRoot = Split-Path -Parent (Split-Path -Parent $VenvPython)
    $configPath = Join-Path $venvRoot 'pyvenv.cfg'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $homeLine = Get-Content -LiteralPath $configPath -Encoding UTF8 | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($homeLine) {
            $venvBaseDir = ($homeLine -replace '^\s*home\s*=\s*', '').Trim()
            $basePython = Join-Path $venvBaseDir 'python.exe'
            if (Test-Path -LiteralPath $basePython -PathType Leaf) { return Get-CanonicalPath $basePython }
        }
    }
    return Get-CanonicalPath $VenvPython
}
function Test-ExpectedDevApiProcess($ProcessInfo, [string]$VenvPython, [int]$Port, [int]$ParentProcessId = 0) {
    if (-not $ProcessInfo -or -not $ProcessInfo.ExecutablePath -or -not $ProcessInfo.CommandLine) { return $false }
    if ($ParentProcessId -gt 0 -and [int]$ProcessInfo.ParentProcessId -ne $ParentProcessId) { return $false }
    $actualExecutable = Get-CanonicalPath $ProcessInfo.ExecutablePath
    $venvExecutable = Get-CanonicalPath $VenvPython
    $baseExecutable = Get-VenvBasePython $VenvPython
    $command = [string]$ProcessInfo.CommandLine
    $isVenvProcess = $actualExecutable -eq $venvExecutable
    $isVenvChild = $actualExecutable -eq $baseExecutable -and $command.IndexOf($venvExecutable, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not ($isVenvProcess -or $isVenvChild)) { return $false }
    return ($command -like '*-m uvicorn backend.app:app*' -and $command -like "*--port $Port*")
}
function Get-Health([string]$Url) {
    try { Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2 } catch { $null }
}
function Get-RuntimeStatus([string]$Url) {
    try { Invoke-RestMethod -Uri "$Url/api/runtime/status" -TimeoutSec 2 } catch { $null }
}
function Test-ProfileRuntime([string]$Url, [string]$ExpectedProfile, [string[]]$ExpectedPolicies = @('offline'), [string[]]$ExpectedInstanceIds = @()) {
    $status = Get-RuntimeStatus $Url
    if (-not $status -or $status.profile -ne $ExpectedProfile) { return $false }
    if ($ExpectedProfile -ne 'prod' -and $status.external_effects.policy -notin $ExpectedPolicies) { return $false }
    if ($ExpectedInstanceIds.Count -gt 0 -and $status.instance_id -notin $ExpectedInstanceIds) { return $false }
    return $true
}
function Show-ProdPendingNotice($Health) {
    if ($Profile -ne 'PROD' -or $Health.release_status -ne 'pending_acceptance') { return }
    $configuredApp = Get-CanonicalPath ([string]$config.profiles.PROD.app)
    $promotionPath = Join-Path (Split-Path -Parent $configuredApp) 'PROMOTION.json'
    if (Test-Path -LiteralPath $promotionPath -PathType Leaf) {
        try {
            $promotion = Get-Content -LiteralPath $promotionPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($promotion.status -in @('promoted', 'promoted_pending_prod_verification', 'prod_verified') -and $promotion.build_id -eq $Health.build_id -and
                $promotion.commit -eq $Health.commit) { return }
        } catch { }
    }
    $message = "当前 PROD 入口连接的是 $($Health.build_id)，发布状态为 pending_acceptance（待验收候选）。`r`n`r`n业务数据：$($Health.storage)`r`n该程序保持现状；运行中心不会自动切换程序或数据。"
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup($message, 0, "格物运行中心 · $($Health.build_id) 待验收候选", 64)
}
function Test-AppManifestFiles([string]$AppPath, $Manifest) {
    if (-not $Manifest.files) { throw "Build manifest has no file inventory: $AppPath" }
    $expected = @($Manifest.files.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($relative in $expected) {
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') {
            throw "Build manifest has an unsafe path: $relative"
        }
        $file = Join-Path $AppPath $relative.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Build file is missing: $relative" }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$Manifest.files.$relative).ToLowerInvariant()) {
            throw "Build file hash does not match manifest: $relative"
        }
    }
    $actual = @(Get-ChildItem -LiteralPath $AppPath -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($AppPath.Length).TrimStart('\').Replace('\', '/')
    } | Where-Object { $_ -ne 'build-manifest.json' })
    if ($actual.Count -ne $expected.Count -or @($actual | Where-Object { $_ -notin $expected }).Count -gt 0) {
        throw "App contains files outside its build manifest: $AppPath"
    }
}
function Test-FrozenListenerIdentity([int]$ProcessId, $ExpectedManifest) {
    $proc = Get-ProcessIdentity $ProcessId
    if (-not $proc -or -not $proc.ExecutablePath -or -not $proc.CommandLine) { return $false }
    $root = Get-CanonicalPath (Split-Path -Parent (Split-Path -Parent $proc.ExecutablePath))
    $expectedExe = Get-CanonicalPath (Join-Path $root 'runtime\pythonw.exe')
    $expectedBootstrap = Join-Path $root 'scripts\desktop_bootstrap.py'
    if ((Get-CanonicalPath $proc.ExecutablePath) -ne $expectedExe -or
        $proc.CommandLine -notlike "*$expectedBootstrap*") { return $false }
    $manifestPath = Join-Path $root 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
    try {
        $actual = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($actual.build_id -ne $ExpectedManifest.build_id -or $actual.commit -ne $ExpectedManifest.commit -or
            $actual.source_sha256 -ne $ExpectedManifest.source_sha256) { return $false }
        Test-AppManifestFiles $root $actual
        return $true
    } catch { return $false }
}
function Get-ProfileConfig($Config, [string]$Name) { $Config.profiles.$Name }
function Assert-PortAvailable([int]$Port, [string]$ExpectedUrl, [string]$ExpectedStorage, [string]$ExpectedBuildId = '') {
    $owner = Get-PortOwner $Port
    if (-not $owner) { return }
    $health = Get-Health $ExpectedUrl
    $storageMatches = $health -and $health.storage -and (Get-CanonicalPath $health.storage) -eq (Get-CanonicalPath $ExpectedStorage)
    $buildMatches = -not $ExpectedBuildId -or ($health.build_id -eq $ExpectedBuildId)
    if (-not $health -or $health.application -ne 'product-tagging-workbench' -or -not $storageMatches -or -not $buildMatches) {
        throw "Port $Port is occupied by PID $owner and its service identity does not match this $Profile entry. Nothing was stopped."
    }
}
function Test-RecordedProcess($Record, [string]$Role) {
    $item = $Record.processes | Where-Object role -eq $Role | Select-Object -First 1
    if (-not $item) { return $null }
    $proc = Get-ProcessIdentity ([int]$item.pid)
    if (-not $proc) { return $null }
    $expectedExe = Get-CanonicalPath $item.executable
    $actualExe = if ($proc.ExecutablePath) { Get-CanonicalPath $proc.ExecutablePath } else { '' }
    if ($actualExe -ne $expectedExe -or -not $proc.CommandLine -or $proc.CommandLine -notlike "*$($item.identity_token)*") {
        throw "Recorded $Role PID $($item.pid) no longer matches launcher identity. It was left running."
    }
    $actualCreation = Get-CreationUtcTicks $proc
    if ($item.creation_utc_ticks -and $actualCreation -and
        [Math]::Abs([double]$actualCreation - [double]$item.creation_utc_ticks) -gt 10000000) {
        throw "Recorded $Role PID $($item.pid) has a different process start time. It was left running."
    }
    return $proc
}
function Write-BrowserOpenLog([string]$Message) {
    try {
        $logPath = Join-Path $runCenterRoot "logs\$Profile\browser-open.log"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "$(Get-Date -Format o) $Message"
    } catch { }
}
function Open-RunCenterPage([string]$Url) {
    $browserExe = $null
    $browserName = $null
    try {
        $choice = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' -Name ProgId -ErrorAction Stop
        $commandPath = "Registry::HKEY_CLASSES_ROOT\$($choice.ProgId)\shell\open\command"
        $openCommand = [string](Get-Item -LiteralPath $commandPath -ErrorAction Stop).GetValue('')
        if ($openCommand -match '^\s*"([^"]+\.exe)"') { $browserExe = $Matches[1] }
        elseif ($openCommand -match '^\s*(.+?\.exe)(?:\s|$)') { $browserExe = $Matches[1].Trim('"') }
        if ($browserExe) { $browserName = [IO.Path]::GetFileName($browserExe).ToLowerInvariant() }
    } catch { }

    try {
        if ($browserName -in @('chrome.exe', 'msedge.exe', 'brave.exe', 'vivaldi.exe', 'opera.exe')) {
            Start-Process -FilePath $browserExe -ArgumentList @('--new-window', $Url) -WindowStyle Normal -ErrorAction Stop | Out-Null
            Write-BrowserOpenLog "Opened $Url in a new $browserName window."
            return $true
        }
        if ($browserName -eq 'firefox.exe') {
            Start-Process -FilePath $browserExe -ArgumentList @('-new-window', $Url) -WindowStyle Normal -ErrorAction Stop | Out-Null
            Write-BrowserOpenLog "Opened $Url in a new Firefox window."
            return $true
        }

        Start-Process -FilePath $Url -ErrorAction Stop | Out-Null
        Write-BrowserOpenLog "Opened $Url through the default URL handler ($browserName)."
        return $true
    } catch {
        $firstError = $_.Exception.Message
        try {
            Start-Process -FilePath $Url -ErrorAction Stop | Out-Null
            Write-BrowserOpenLog "New-window launch failed ($firstError); opened $Url through the default URL handler."
            return $true
        } catch {
            Write-BrowserOpenLog "Browser launch failed for ${Url}: $($_.Exception.Message)"
            Write-Warning "DEV is ready, but the browser could not be opened automatically. Open $Url manually."
            return $false
        }
    }
}
function Save-Record($Record, [string]$Path) {
    $tmp = "$Path.tmp"
    $Record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
function New-RunRecord([string]$Name, [string]$Url, [string]$Data, [string]$Cache, [string]$BuildId, [string]$Commit, [string]$ProjectRoot, $Processes) {
    [ordered]@{
        schema = 1; profile = $Name; started_at = (Get-Date).ToUniversalTime().ToString('o'); url = $Url
        project_root = $ProjectRoot; data = $Data; cache = $Cache; build_id = $BuildId; commit = $Commit
        processes = @($Processes)
    }
}
function Add-ProcessRecord($List, [string]$Role, [System.Diagnostics.Process]$Process, [string]$Executable, [string]$IdentityToken) {
    $creation = $null
    try { $creation = $Process.StartTime.ToUniversalTime().Ticks } catch { }
    $List.Add([pscustomobject]@{ role = $Role; pid = $Process.Id; executable = $Executable; identity_token = $IdentityToken; creation_utc_ticks = $creation })
}

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Run Center is not installed. Run scripts\install-run-center.ps1 first. Missing: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = Get-ProfileConfig $config $Profile
if (-not $entry) { throw "No $Profile profile is configured in $ConfigPath" }
$devCandidateBundle = $null
if ($DevCandidateReport) {
    if ($Profile -ne 'DEV') { throw '-DevCandidateReport is valid only with -Profile DEV.' }
    if ($ExternalEffectsMode -ne 'offline') { throw 'Frozen DEV candidates may only start with -ExternalEffectsMode offline.' }
    $gateModule = Join-Path $runCenterRoot 'dev-release-gates.psm1'
    if (-not (Test-Path -LiteralPath $gateModule -PathType Leaf)) { throw "Run Center DEV release gate module is missing: $gateModule" }
    Import-Module $gateModule -Force
    $reports = [IO.Path]::GetFullPath([string]$config.profiles.DEV.reports)
    $builds = Join-Path ([IO.Path]::GetFullPath([string]$config.projectRoot)) 'builds'
    $devCandidateBundle = Test-DgCandidateBundle $DevCandidateReport $reports $builds
    $DevCandidateReport = $devCandidateBundle.report_path
}
$runRoot = Join-Path $runCenterRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$recordPath = Join-Path $runRoot "$Profile.json"
$record = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
if ($Action -eq 'Stop' -and $DevCandidateReport -and
    (-not $record -or [IO.Path]::GetFullPath([string]$record.dev_candidate_report) -ine $DevCandidateReport)) {
    throw 'The requested DEV candidate does not match the Run Center process record; nothing was stopped.'
}

if ($Action -eq 'Status') {
    $url = if ($devCandidateBundle) { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url }
    $healthUrl = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $url }
    $health = Get-Health $healthUrl
    $runtime = Get-RuntimeStatus $healthUrl
    $runtimeIdentity = if ($runtime) { "$($runtime.profile)/$($runtime.external_effects.policy); instance_id=$($runtime.instance_id)" } else { 'runtime-status-unavailable' }
    [pscustomobject]@{ profile = $Profile; url = $url; api_url = $healthUrl; page_status = if ($Profile -eq 'DEV' -and -not ($record -and $record.dev_candidate_report)) { if (Get-PortOwner ([int]$entry.webPort)) { 'listening' } else { 'stopped' } } else { if ($health) { 'ready' } else { 'stopped' } }; api_status = if ($health) { $health.status } else { 'stopped' }; runtime = $runtimeIdentity; build_id = $health.build_id; commit = $health.commit; storage = $health.storage; candidate_report = if ($record) { $record.dev_candidate_report } else { $null }; record = $recordPath } | Format-List
    exit 0
}

# Starts hold this exclusive lock until their listeners pass identity checks.
# Maintenance scripts take the same lock before stopping or moving TEST files.
$startupLock = $null
if ($Action -eq 'Start') {
    $lockPath = Join-Path $runCenterRoot 'maintenance.lock'
    try { $startupLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another Run Center start or TEST maintenance operation is active. Nothing was started.' }
}

$devRuntimePolicy = if ($ExternalEffectsMode -eq 'offline') { 'offline' } else { 'live_validation' }
$devRuntimeInstanceId = 'DEV'
$testRuntimePolicy = if ($ExternalEffectsMode -eq 'offline') { 'offline' } else { 'live_validation' }
$testRuntimeInstanceId = 'TEST'
# Desktop entry starts DEV and TEST online without a prompt. The explicit offline value remains
# available to deterministic candidate-freeze and maintenance scripts.

if ($Action -eq 'Start' -and $record) {
    $healthUrl = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url }
    $runningHealth = Get-Health $healthUrl
    # Reconcile a detached TEST runtime child only when its recorded Workbench launcher,
    # current health identity, parent PID, bootstrap path, and frozen manifest all agree.
    # This allows Run Center Stop/Start to manage the exact child without adopting unknown listeners.
    if ($Profile -eq 'TEST' -and $runningHealth -and $runningHealth.application -eq 'product-tagging-workbench' -and
        (Get-CanonicalPath $runningHealth.storage) -eq (Get-CanonicalPath $record.data) -and
        $runningHealth.build_id -eq $record.build_id -and $runningHealth.commit -eq $record.commit) {
        $runtimeStatus = Get-RuntimeStatus $entry.url
        $serviceOwner = Get-PortOwner ([int]$entry.port)
        $knownPids = @($record.processes | ForEach-Object { [int]$_.pid })
        $launcherRecord = $record.processes | Where-Object role -eq 'app' | Select-Object -First 1
        if ($serviceOwner -and $serviceOwner -notin $knownPids -and $launcherRecord -and
            $runtimeStatus -and $runtimeStatus.profile -eq 'test' -and $runtimeStatus.instance_id -eq 'TEST' -and
            $runtimeStatus.external_effects.policy -in @('offline', 'live_validation')) {
            $listenerIdentity = Get-ProcessIdentity ([int]$serviceOwner)
            $appDir = Get-CanonicalPath $entry.app
            $expectedRuntime = Get-CanonicalPath (Join-Path $appDir 'runtime\pythonw.exe')
            $expectedBootstrap = Join-Path $appDir 'scripts\desktop_bootstrap.py'
            $manifestPath = Join-Path $appDir 'build-manifest.json'
            $manifest = if (Test-Path -LiteralPath $manifestPath -PathType Leaf) { Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
            if ($listenerIdentity -and [int]$listenerIdentity.ParentProcessId -eq [int]$launcherRecord.pid -and
                $listenerIdentity.ExecutablePath -and (Get-CanonicalPath $listenerIdentity.ExecutablePath) -eq $expectedRuntime -and
                $listenerIdentity.CommandLine -like "*$expectedBootstrap*" -and $manifest -and
                $manifest.build_id -eq $record.build_id -and $manifest.commit -eq $record.commit -and
                (Test-FrozenListenerIdentity ([int]$serviceOwner) $manifest)) {
                $listenerObject = Get-Process -Id ([int]$serviceOwner) -ErrorAction SilentlyContinue
                if ($listenerObject) {
                    $processList = [System.Collections.Generic.List[object]]::new()
                    foreach ($item in $record.processes) { $processList.Add($item) }
                    Add-ProcessRecord $processList 'app-runtime' $listenerObject $expectedRuntime $expectedBootstrap
                    $record.processes = @($processList.ToArray())
                    Save-Record $record $recordPath
                }
            }
        }
    }
    if ($Profile -eq 'DEV' -and -not $record.dev_candidate_report -and $runningHealth -and $runningHealth.application -eq 'product-tagging-workbench' -and
        (Get-CanonicalPath $runningHealth.storage) -eq (Get-CanonicalPath $record.data)) {
        $apiOwner = Get-PortOwner ([int]$entry.apiPort)
        $knownPids = @($record.processes | ForEach-Object { [int]$_.pid })
        if ($apiOwner -and $apiOwner -notin $knownPids) {
            $apiLauncher = $record.processes | Where-Object role -eq 'api' | Select-Object -First 1
            $webOwner = Get-PortOwner ([int]$entry.webPort)
            $webRecord = $record.processes | Where-Object role -eq 'web' | Select-Object -First 1
            $venvPython = Join-Path (Get-CanonicalPath $config.projectRoot) '.venv\Scripts\python.exe'
            $listenerIdentity = Get-ProcessIdentity ([int]$apiOwner)
            if ($apiLauncher -and $webRecord -and $webOwner -eq [int]$webRecord.pid -and
                (Test-ExpectedDevApiProcess $listenerIdentity $venvPython ([int]$entry.apiPort) ([int]$apiLauncher.pid))) {
                $listenerObject = Get-Process -Id ([int]$apiOwner) -ErrorAction SilentlyContinue
                if ($listenerObject) {
                    $processList = [System.Collections.Generic.List[object]]::new()
                    foreach ($item in $record.processes) { $processList.Add($item) }
                    Add-ProcessRecord $processList 'api-listener' $listenerObject $listenerIdentity.ExecutablePath "--port $($entry.apiPort)"
                    $record.processes = @($processList.ToArray())
                    $record.build_id = $runningHealth.build_id
                    $record.commit = $runningHealth.commit
                    $record.project_root = $config.projectRoot
                    Save-Record $record $recordPath
                }
            }
        }
    }
    $recordMatches = $runningHealth -and $runningHealth.application -eq 'product-tagging-workbench' -and
        (Get-CanonicalPath $runningHealth.storage) -eq (Get-CanonicalPath $record.data) -and
        $runningHealth.commit -eq $record.commit -and $runningHealth.build_id -eq $record.build_id
    if ($Profile -eq 'DEV' -and $devCandidateBundle) {
        $recordMatches = $recordMatches -and [IO.Path]::GetFullPath([string]$record.dev_candidate_report) -ieq $devCandidateBundle.report_path -and
            [string]$record.dev_candidate_report_sha256 -eq $devCandidateBundle.report_sha256
    } elseif ($Profile -eq 'DEV' -and $record.dev_candidate_report) { $recordMatches = $false }
    if ($recordMatches -and $Profile -ne 'PROD') {
        $expectedPolicy = if ($Profile -eq 'TEST') { @($testRuntimePolicy) } else { @($devRuntimePolicy) }
        $expectedInstance = if ($Profile -eq 'TEST') { @($testRuntimeInstanceId) } else { @($devRuntimeInstanceId) }
        $recordMatches = Test-ProfileRuntime $healthUrl $Profile.ToLowerInvariant() $expectedPolicy $expectedInstance
    }
    if ($recordMatches) {
        $servicePort = if ($Profile -eq 'DEV') { [int]$entry.apiPort } else { [int]$entry.port }
        $serviceOwner = Get-PortOwner $servicePort
        if (-not $serviceOwner -or $serviceOwner -notin @($record.processes | ForEach-Object { [int]$_.pid })) {
            throw "$Profile is responding, but the listener PID is not part of its recorded process identity. It was left running."
        }
        foreach ($processRole in $record.processes.role | Select-Object -Unique) {
            $null = Test-RecordedProcess $record $processRole
        }
        if ($Profile -eq 'DEV' -and -not $record.dev_candidate_report) {
            $webOwner = Get-PortOwner ([int]$entry.webPort)
            if (-not $webOwner -or $webOwner -notin @($record.processes | Where-Object role -eq 'web' | ForEach-Object { [int]$_.pid })) {
                throw 'DEV API is running but the recorded Vite listener is unavailable or no longer matches. Stop the owned entry before restarting.'
            }
        }
        if (-not $NoOpen) { Open-RunCenterPage $(if ($record.dev_candidate_report) { $healthUrl } else { $entry.url }) | Out-Null; Show-ProdPendingNotice $runningHealth }
        Write-Output "$Profile is already running with the recorded identity: $($record.build_id) / $($record.commit)"
        exit 0
    }
}

if ($Action -eq 'Stop') {
    $stopTracePath = Join-Path $runCenterRoot "logs\$Profile\ui-stop-runner.log"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stopTracePath) | Out-Null
    function Write-StopTrace([string]$Message) {
        "$(Get-Date -Format o) $Message" | Add-Content -LiteralPath $stopTracePath -Encoding UTF8
    }
    Write-StopTrace "Stop entered; profile=$Profile; record_exists=$([bool]$record)"
    if (-not $record) { Write-Output "No launcher-owned $Profile instance is recorded."; exit 0 }
    $url = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url }
    $servicePort = if ($Profile -eq 'DEV') { [int]$entry.apiPort } else { [int]$entry.port }
    # Verify every surviving recorded PID before sending a stop request. A matching health URL
    # alone is insufficient: another process could have taken over the port/data directory.
    foreach ($role in $record.processes.role | Select-Object -Unique) { $null = Test-RecordedProcess $record $role }
    Write-StopTrace 'Recorded process identities verified.'
    $serviceOwner = Get-PortOwner $servicePort
    if ($serviceOwner) {
        $ownerRecord = $record.processes | Where-Object { [int]$_.pid -eq [int]$serviceOwner } | Select-Object -First 1
        if (-not $ownerRecord -and $Profile -eq 'DEV' -and $record.dev_candidate_report) {
            # A candidate may have started its embedded runtime before a failed policy check
            # wrote the final process record. Recover only this exact recorded app's child.
            $gateModule = Join-Path $runCenterRoot 'dev-release-gates.psm1'
            Import-Module $gateModule -Force -ErrorAction Stop
            $bundle = Test-DgCandidateBundle ([string]$record.dev_candidate_report) ([string]$config.profiles.DEV.reports) (Join-Path ([string]$config.projectRoot) 'builds')
            $recordedApp = $record.processes | Where-Object role -eq 'app' | Select-Object -First 1
            $appProcess = Test-RecordedProcess $record 'app'
            $child = Get-ProcessIdentity ([int]$serviceOwner)
            $runtimeProcess = Get-Process -Id ([int]$serviceOwner) -ErrorAction Stop
            $childCreated = $runtimeProcess.StartTime.ToUniversalTime().Ticks
            $expectedBootstrap = Join-Path $bundle.candidate_app 'scripts\desktop_bootstrap.py'
            if (-not $recordedApp -or -not $child -or [int]$child.ParentProcessId -ne [int]$recordedApp.pid -or
                (Get-CanonicalPath ([string]$recordedApp.executable)) -ne (Join-Path $bundle.candidate_app 'Workbench.exe') -or
                ($appProcess -and [int]$appProcess.ProcessId -ne [int]$recordedApp.pid) -or
                -not $recordedApp.creation_utc_ticks -or
                $childCreated -lt [long]$recordedApp.creation_utc_ticks -or
                $childCreated -gt ([long]$recordedApp.creation_utc_ticks + [TimeSpan]::FromMinutes(5).Ticks) -or
                [string]$record.dev_candidate_report_sha256 -ne $bundle.report_sha256 -or
                (Get-CanonicalPath ([string]$record.candidate_app)) -ne $bundle.candidate_app -or
                -not (Test-FrozenListenerIdentity ([int]$serviceOwner) $bundle.manifest)) {
                throw 'DEV candidate listener is not the exact recorded app child and frozen bundle. Nothing was stopped.'
            }
            $runtimeRecord = [pscustomobject]@{
                role = 'app-runtime'; pid = [int]$serviceOwner
                executable = (Join-Path $bundle.candidate_app 'runtime\pythonw.exe')
                identity_token = $expectedBootstrap; creation_utc_ticks = $childCreated
            }
            $record.processes = @($record.processes) + @($runtimeRecord)
            Save-Record $record $recordPath
            $ownerRecord = $runtimeRecord
        }
        if (-not $ownerRecord) { throw "Port $servicePort is owned by PID $serviceOwner, which is not recorded for this $Profile instance. Nothing was stopped." }
        $null = Test-RecordedProcess $record $ownerRecord.role
        $owner = Get-ProcessIdentity ([int]$serviceOwner)
        if (-not $owner -or -not $owner.ExecutablePath) { throw "Cannot verify the listener process for port $servicePort. Nothing was stopped." }
    }
    $health = Get-Health $url
    if ($serviceOwner -and -not $health -and $Profile -ne 'DEV') { throw "Port $servicePort is owned by a recorded process but does not return its health identity. Nothing was stopped." }
    if (-not $serviceOwner -and $health) { throw "Health responded on $url without a matching listener snapshot. Nothing was stopped." }
    if ($health -and ($health.application -ne 'product-tagging-workbench' -or
        (Get-CanonicalPath $health.storage) -ne (Get-CanonicalPath $record.data) -or
        $health.build_id -ne $record.build_id -or $health.commit -ne $record.commit)) {
        throw "Service identity on $url does not match the recorded $Profile process, build, and data path. Nothing was stopped."
    }
    if ($Profile -eq 'DEV' -and -not $record.dev_candidate_report) {
        # Close Vite before stopping the API. When this script is launched by the API,
        # stopping the API can terminate its launcher process on Windows before it gets
        # a chance to close the browser-facing listener.
        $webOwner = Get-PortOwner ([int]$entry.webPort)
        if ($webOwner) {
            $webRecord = $record.processes | Where-Object { $_.role -eq 'web' -and [int]$_.pid -eq [int]$webOwner } | Select-Object -First 1
            if (-not $webRecord) { throw "DEV web port $($entry.webPort) is owned by an unrecorded process. Nothing was stopped." }
            $webProc = Test-RecordedProcess $record 'web'
            if (-not $webProc) { throw 'The recorded Vite process is no longer available; the port remains occupied.' }
            Stop-Process -Id ([int]$webProc.ProcessId) -ErrorAction Stop
            Write-StopTrace "Stopped recorded web process pid=$($webProc.ProcessId)."
            for ($i=0; $i -lt 20; $i++) { if (-not (Get-PortOwner ([int]$entry.webPort))) { break }; Start-Sleep -Milliseconds 250 }
            if (Get-PortOwner ([int]$entry.webPort)) { throw "DEV web port $($entry.webPort) remains occupied. The process record was retained." }
        }
        Write-StopTrace 'DEV web listener released.'
    }
    $released = -not $serviceOwner
    if ($health) {
        if ($Profile -ne 'PROD') {
            $allowedPolicies = @('offline', 'live_validation')
            $allowedInstances = @($Profile, "$Profile-LIVE-VALIDATION")
            if (-not (Test-ProfileRuntime $url $Profile.ToLowerInvariant() $allowedPolicies $allowedInstances)) {
                throw "Runtime profile or policy does not match this $Profile entry. Nothing was stopped."
            }
        }
        if ($Profile -ne 'DEV' -or $record.dev_candidate_report) {
            try { Invoke-RestMethod -Method Post -Uri "$url/api/server/stop" -TimeoutSec 3 | Out-Null } catch { }
            for ($i=0; $i -lt 20; $i++) {
                if (-not (Get-PortOwner $servicePort)) { $released = $true; break }
                Start-Sleep -Milliseconds 500
            }
        }
    }
    if (-not $released -and $Profile -eq 'DEV') {
        if ($record.dev_candidate_report) {
            throw 'The packaged DEV candidate did not stop gracefully. Its app-runtime identity is retained; no source-DEV process termination was attempted.'
        }
        # The DEV API may be unresponsive while its exact listener is still alive. After the
        # graceful stop window, terminate only API processes that still match recorded PID,
        # executable, command-line token, and creation time. Never touch an unrecorded owner.
        # Stop the recorded listener/reloader before its launcher parent so it cannot
        # respawn a child or retain the bound socket during shutdown.
        $apiRoles = @('api-listener', 'api-child', 'api')
        $verifiedApiPids = @()
        foreach ($role in $apiRoles) {
            $verifiedApiProcess = Test-RecordedProcess $record $role
            if ($verifiedApiProcess) { $verifiedApiPids += [int]$verifiedApiProcess.ProcessId }
        }
        if ([int]$serviceOwner -notin $verifiedApiPids) {
            throw "DEV API port $servicePort is not owned by a verified recorded API process. Nothing was stopped."
        }
        foreach ($role in $apiRoles) {
            $verifiedApiProcess = Test-RecordedProcess $record $role
            if ($verifiedApiProcess) {
                Stop-Process -Id ([int]$verifiedApiProcess.ProcessId) -ErrorAction Stop
                Write-StopTrace "Stopped recorded API process role=$role pid=$($verifiedApiProcess.ProcessId)."
            }
        }
        for ($i=0; $i -lt 4; $i++) {
            if (-not (Get-PortOwner $servicePort)) { $released = $true; break }
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $released) { throw "The recorded $Profile service did not release port $servicePort. Its process record was retained." }
    Write-StopTrace 'API listener released.'
    Remove-Item -LiteralPath $recordPath -Force
    Write-StopTrace 'Runtime record removed; stop completed.'
    Write-Output "Stopped only matching launcher-recorded $Profile processes."
    exit 0
}

$dataPath = Get-CanonicalPath $entry.data
$cachePath = Get-CanonicalPath $entry.cache
if ($Profile -eq 'PROD') {
    if ($dataPath -ne (Get-CanonicalPath 'K:\Bobbytagging\Data') -or
        $cachePath -ne (Get-CanonicalPath 'K:\Bobbytagging\Cache')) {
        throw 'PROD is pinned to the existing formal Data/Cache paths. Refusing a changed path.'
    }
    if ((Get-CanonicalPath $entry.app) -notmatch '^K:\\Bobbytagging\\Candidate-beta\.[0-9]+\\app$') {
        throw 'PROD app must be a versioned Candidate-beta.N app directory promoted through the acceptance gate.'
    }
    $promotionPath = Join-Path (Split-Path -Parent (Get-CanonicalPath $entry.app)) 'PROMOTION.json'
    if (-not (Test-Path -LiteralPath $promotionPath -PathType Leaf)) { throw "PROD promotion evidence is missing: $promotionPath" }
    $promotion = Get-Content -LiteralPath $promotionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($promotion.status -notin @('promoted', 'promoted_pending_prod_verification', 'prod_verified') -or [IO.Path]::GetFullPath([string]$promotion.target_app) -ine (Get-CanonicalPath $entry.app) -or
        [IO.Path]::GetFullPath([string]$promotion.run_center_config) -ine (Get-CanonicalPath $ConfigPath) -or
        [IO.Path]::GetFullPath([string]$promotion.prod_data_path) -ine $dataPath -or
        [IO.Path]::GetFullPath([string]$promotion.prod_cache_path) -ine $cachePath -or
        -not $promotion.manual_result -or -not $promotion.manual_result_sha256 -or -not $promotion.portable_zip -or -not $promotion.portable_sha256) {
        throw 'PROD promotion evidence is incomplete or does not identify the configured app.'
    }
    Import-Module (Join-Path $PSScriptRoot 'dev-release-gates.psm1') -Force
    $backupGate = Test-DgProdBackupReadiness ([string]$promotion.prod_backup_report) 'K:\Bobbytagging\Backups' $dataPath $cachePath
    if ([string]$promotion.prod_backup_report_sha256 -ne $backupGate.report_sha256 -or
        [IO.Path]::GetFullPath([string]$promotion.prod_database_backup) -ine $backupGate.database_backup -or
        [IO.Path]::GetFullPath([string]$promotion.prod_reasonix_backup) -ine $backupGate.reasonix_backup -or
        [IO.Path]::GetFullPath([string]$promotion.prod_restore_root) -ine $backupGate.restore_root -or
        [IO.Path]::GetFullPath([string]$promotion.prod_restored_database) -ine $backupGate.restored_database) {
        throw 'PROD promotion backup and isolated restore evidence is missing or changed.'
    }
    foreach ($bound in @(
        @{ path = [string]$promotion.manual_result; hash = [string]$promotion.manual_result_sha256 },
        @{ path = [string]$promotion.portable_zip; hash = [string]$promotion.portable_sha256 },
        @{ path = [string]$promotion.portable_sidecar; hash = [string]$promotion.portable_sidecar_sha256 },
        @{ path = [string]$promotion.candidate_report; hash = [string]$promotion.candidate_report_sha256 }
    )) {
        if (-not (Test-Path -LiteralPath $bound.path -PathType Leaf) -or
            (Get-FileHash -LiteralPath $bound.path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $bound.hash.ToLowerInvariant()) {
            throw "PROD promotion evidence file is missing or changed: $($bound.path)"
        }
    }
    $manualResult = Get-Content -LiteralPath $promotion.manual_result -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manualResult.status -ne 'passed' -or $manualResult.build_id -ne $promotion.build_id -or
        $manualResult.commit -ne $promotion.commit -or $manualResult.version -ne $promotion.version -or
        $manualResult.portable_sha256 -ne $promotion.portable_sha256 -or
        $manualResult.portable_sidecar_sha256 -ne $promotion.portable_sidecar_sha256 -or
        $manualResult.candidate_report_sha256 -ne $promotion.candidate_report_sha256 -or
        $manualResult.app_manifest_sha256 -ne $promotion.app_manifest_sha256) {
        throw 'PROD promotion does not match a passing manual acceptance record for this portable build.'
    }
}
foreach ($otherProfile in @('DEV', 'TEST', 'PROD') | Where-Object { $_ -ne $Profile }) {
    $otherData = Get-CanonicalPath $config.profiles.$otherProfile.data
    $otherCache = Get-CanonicalPath $config.profiles.$otherProfile.cache
    if ((Test-PathOverlap $dataPath $otherData) -or (Test-PathOverlap $dataPath $otherCache) -or
        (Test-PathOverlap $cachePath $otherData) -or (Test-PathOverlap $cachePath $otherCache)) {
        throw "$Profile data/cache paths overlap $otherProfile data/cache. Refusing to launch."
    }
}
if ($dataPath -eq $cachePath) { throw 'Data and cache directories must be separate.' }
if ($Profile -eq 'PROD') {
    if (-not (Test-Path -LiteralPath $dataPath -PathType Container) -or -not (Test-Path -LiteralPath $cachePath -PathType Container)) {
        throw "PROD data/cache must already exist. The launcher will not create or initialize formal data paths."
    }
} else {
    New-Item -ItemType Directory -Force -Path $dataPath, $cachePath | Out-Null
}

$processes = [System.Collections.Generic.List[object]]::new()
$buildId = ''
$env:GEWU_PROFILE = $Profile.ToLowerInvariant()
if ($Profile -eq 'PROD') { Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
if ($Profile -eq 'DEV' -and -not $DevCandidateReport) {
    $root = Get-CanonicalPath $config.projectRoot
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $node = $config.nodeExe
    $vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $python)) { throw "Development Python is missing: $python" }
    if (-not $node -or -not (Test-Path -LiteralPath $node)) { throw 'Node.js was not found. Set nodeExe in Run Center config.' }
    if (-not (Test-Path -LiteralPath $vite)) { throw "Vite is not installed: $vite" }
    $apiPort = [int]$entry.apiPort
    $webPort = [int]$entry.webPort
    Assert-PortAvailable $apiPort "http://127.0.0.1:$apiPort" $dataPath
    if (Get-PortOwner $apiPort) { throw "DEV API port $apiPort already has a matching listener but is not owned by this launch. Nothing was started or stopped." }
    $webOwner = Get-PortOwner $webPort
    if ($webOwner) { throw "DEV web port $webPort is already owned by PID $webOwner. Nothing was stopped." }
    $logRoot = Join-Path $runCenterRoot 'logs\DEV'
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $env:PYTHONUTF8 = '1'; $env:TAG_STORAGE = $dataPath; $env:TAG_CACHE = $cachePath
    $env:TAG_PROVIDER_HOME = if ($entry.providerHome) { [IO.Path]::GetFullPath([string]$entry.providerHome) } else { Join-Path $dataPath 'providers' }
    $env:GEWU_API_URL = "http://127.0.0.1:$apiPort"
    $env:GEWU_FRONTEND_URL = "http://127.0.0.1:$webPort"
    $env:GEWU_INSTANCE_ID = 'DEV'
    $env:GEWU_REPORTS_DIR = $entry.reports
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    # DEV code is loaded on each Run Center start. Avoid uvicorn --reload here:
    # its orphanable reloader/worker tree makes an identity-checked Stop leave port 8896 alive.
    $apiArgs = @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$apiPort")
    $previousDevExternalEffects = [Environment]::GetEnvironmentVariable('GEWU_DEV_EXTERNAL_EFFECTS', 'Process')
    $previousTestExternalEffects = [Environment]::GetEnvironmentVariable('GEWU_TEST_EXTERNAL_EFFECTS', 'Process')
    try {
        Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
        Remove-Item Env:GEWU_TEST_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
        if ($devRuntimePolicy -eq 'offline') { $env:GEWU_DEV_EXTERNAL_EFFECTS = 'offline' }
        $api = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'api.out.log') -RedirectStandardError (Join-Path $logRoot 'api.err.log') -PassThru
    } finally {
        if ($null -eq $previousDevExternalEffects) { Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
        else { $env:GEWU_DEV_EXTERNAL_EFFECTS = $previousDevExternalEffects }
        if ($null -eq $previousTestExternalEffects) { Remove-Item Env:GEWU_TEST_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
        else { $env:GEWU_TEST_EXTERNAL_EFFECTS = $previousTestExternalEffects }
    }
    Add-ProcessRecord $processes 'api' $api $python "--port $apiPort"
    $record = New-RunRecord $Profile "http://127.0.0.1:$webPort" $dataPath $cachePath '' '' $root $processes
    Save-Record $record $recordPath
    $env:GEWU_DEV_API_URL = "http://127.0.0.1:$apiPort"
    $apiReady = $false
    for ($i=0; $i -lt 90; $i++) { if (Get-Health "http://127.0.0.1:$apiPort") { $apiReady = $true; break }; Start-Sleep -Milliseconds 500 }
    if (-not $apiReady) { throw "DEV API did not become ready. Logs: $logRoot" }
    $web = Start-Process -FilePath $node -ArgumentList @($vite,'--host','127.0.0.1','--port',"$webPort") -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'web.out.log') -RedirectStandardError (Join-Path $logRoot 'web.err.log') -PassThru
    Add-ProcessRecord $processes 'web' $web $node 'vite.js'
    $record = New-RunRecord $Profile "http://127.0.0.1:$webPort" $dataPath $cachePath '' '' $root $processes
    Save-Record $record $recordPath
    $ready = $false
    for ($i=0; $i -lt 90; $i++) { if (Get-PortOwner $webPort) { $ready = $true; break }; Start-Sleep -Milliseconds 500 }
    if (-not $ready) { throw "DEV frontend did not become ready. Logs: $logRoot" }
    $apiChild = Get-CimInstance Win32_Process | Where-Object {
        Test-ExpectedDevApiProcess $_ $python $apiPort $api.Id
    } | Select-Object -First 1
    if ($apiChild) {
        $childProcess = Get-Process -Id $apiChild.ProcessId -ErrorAction SilentlyContinue
        if ($childProcess) { Add-ProcessRecord $processes 'api-child' $childProcess $apiChild.ExecutablePath "--port $apiPort" }
    }
    $apiOwner = Get-PortOwner $apiPort
    if ($apiOwner -notin @($processes | ForEach-Object { [int]$_.pid })) {
        $apiOwnerProcess = if ($apiOwner) { Get-ProcessIdentity ([int]$apiOwner) } else { $null }
        if (-not (Test-ExpectedDevApiProcess $apiOwnerProcess $python $apiPort $api.Id)) {
            throw "DEV API port $apiPort is not owned by the expected backend process. The listener was left running."
        }
        $apiOwnerProcObject = Get-Process -Id ([int]$apiOwner) -ErrorAction SilentlyContinue
        if (-not $apiOwnerProcObject) { throw 'DEV API listener exited before its identity could be recorded.' }
        Add-ProcessRecord $processes 'api-listener' $apiOwnerProcObject $apiOwnerProcess.ExecutablePath "--port $apiPort"
    }
    $entry.url = "http://127.0.0.1:$webPort"
} else {
    $appDir = if ($devCandidateBundle) { $devCandidateBundle.candidate_app } else { Get-CanonicalPath $entry.app }
    $exe = Join-Path $appDir 'Workbench.exe'
    $manifestPath = Join-Path $appDir 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $exe) -or -not (Test-Path -LiteralPath $manifestPath)) { throw "$Profile frozen app or build manifest is missing: $appDir" }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $buildId = $manifest.build_id
    if (-not $buildId) { throw "Build manifest has no build_id: $manifestPath" }
    Test-AppManifestFiles $appDir $manifest
    if ($Profile -eq 'PROD') {
        if ($manifest.version -ne $promotion.version -or $manifest.build_id -ne $promotion.build_id -or
            $manifest.commit -ne $promotion.commit -or
            (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$promotion.app_manifest_sha256).ToLowerInvariant()) {
            throw 'PROD app manifest does not match the promoted and manually accepted candidate.'
        }
    }
    $port = if ($devCandidateBundle) { [int]$entry.apiPort } else { [int]$entry.port }
    if ($devCandidateBundle) { $entry.url = "http://127.0.0.1:$port" }
    if ($devCandidateBundle) {
        $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($manifestHash -ne $devCandidateBundle.manifest_sha256 -or $manifest.build_mode -ne 'candidate' -or
            $manifest.storage_mode -ne 'user-selected' -or $manifest.dirty -ne $false) {
            throw 'DEV frozen app manifest does not match the pending manual-acceptance report.'
        }
    }
    if ($Profile -eq 'TEST') {
        if ($manifest.dirty -ne $false -or $manifest.build_mode -ne 'candidate' -or
            $manifest.storage_mode -ne 'user-selected' -or -not $manifest.commit) {
            throw 'TEST requires a clean, commit-pinned portable candidate build.'
        }
        $reportsRoot = [IO.Path]::GetFullPath([string]$config.profiles.TEST.reports)
        $candidateName = "candidate-$buildId-$($manifest.commit.Substring(0,12)).json"
        $candidateReportFiles = @(Get-ChildItem -LiteralPath $reportsRoot -Filter $candidateName -File -ErrorAction SilentlyContinue)
        if ($candidateReportFiles.Count -ne 1) { throw 'TEST requires one frozen portable candidate report in the acceptance Reports directory.' }
        $candidateReportFile = $candidateReportFiles[0]
        $candidateReport = Get-Content -LiteralPath $candidateReportFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidateZip = [IO.Path]::GetFullPath([string]$candidateReport.portable_zip)
        $candidateSidecar = [IO.Path]::GetFullPath([string]$candidateReport.portable_sidecar)
        if ($candidateReport.status -notin @('activated_pending_acceptance', 'running_pending_manual_acceptance') -or
            $candidateReport.build_id -ne $buildId -or $candidateReport.commit -ne $manifest.commit -or
            [IO.Path]::GetFullPath([string]$candidateReport.current_app) -ine $appDir) {
            throw 'TEST runtime does not match the frozen portable candidate report.'
        }
        if (-not (Test-Path -LiteralPath $candidateZip -PathType Leaf) -or -not (Test-Path -LiteralPath $candidateSidecar -PathType Leaf) -or
            (Get-FileHash -LiteralPath $candidateZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$candidateReport.portable_sha256).ToLowerInvariant() -or
            (Get-FileHash -LiteralPath $candidateSidecar -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$candidateReport.portable_sidecar_sha256).ToLowerInvariant() -or
            (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne ([string]$candidateReport.manifest_sha256).ToLowerInvariant()) {
            throw 'TEST portable ZIP, sidecar or app manifest changed after candidate activation.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $appDir 'backend\external_effects.py')) -or
            -not (Select-String -LiteralPath (Join-Path $appDir 'backend\app.py') -Pattern '/api/runtime/status' -Quiet)) {
            throw 'TEST build predates the offline runtime policy or runtime-status API. Freeze a new TEST candidate first.'
        }
    }
    Assert-PortAvailable $port $entry.url $dataPath $buildId
    $portOwner = Get-PortOwner $port
    if ($portOwner) {
        $health = Get-Health $entry.url
        if ($record) { throw "$Profile port is occupied but the recorded process identity did not match. Nothing was stopped or adopted." }
        if (-not $health -or $health.application -ne 'product-tagging-workbench' -or
            (Get-CanonicalPath $health.storage) -ne $dataPath -or $health.build_id -ne $buildId -or
            $health.commit -ne $manifest.commit -or $health.source_sha256 -ne $manifest.source_sha256) {
            throw "$Profile port has an unmanaged listener that does not match the frozen build and data path. Nothing was stopped."
        }
        if (-not (Test-FrozenListenerIdentity ([int]$portOwner) $manifest)) {
            throw "$Profile listener PID $portOwner does not match the embedded Python/bootstrap identity and verified build manifest. Nothing was stopped or adopted."
        }
        $expectedPolicy = if ($Profile -eq 'TEST') { @($testRuntimePolicy) } else { @($devRuntimePolicy) }
        $expectedInstance = if ($Profile -eq 'TEST') { @($testRuntimeInstanceId) } else { @($devRuntimeInstanceId) }
        if ($Profile -ne 'PROD' -and -not (Test-ProfileRuntime $entry.url $Profile.ToLowerInvariant() $expectedPolicy $expectedInstance)) {
            throw "$Profile port does not match the selected runtime policy/instance. The existing process was left running; nothing was stopped or adopted."
        }
        Write-Output "$Profile is already serving the exact frozen build and data directory, but was not launched by this Run Center. Its process is not claimed for Stop."
        if (-not $NoOpen) { Open-RunCenterPage $entry.url | Out-Null; Show-ProdPendingNotice $health }
        exit 0
    }
    $env:GEWU_PROFILE = $Profile.ToLowerInvariant()
    $env:TAG_PROVIDER_HOME = if ($entry.providerHome) { [IO.Path]::GetFullPath([string]$entry.providerHome) } else { Join-Path $dataPath 'providers' }
    $env:GEWU_API_URL = $entry.url
    $env:GEWU_FRONTEND_URL = $entry.url
    $env:GEWU_REPORTS_DIR = $entry.reports
    if ($Profile -eq 'PROD') { Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
    $args = @('--data',$dataPath,'--cache',$cachePath,'--port',"$port",'--no-open')
    $previousDevExternalEffects = [Environment]::GetEnvironmentVariable('GEWU_DEV_EXTERNAL_EFFECTS', 'Process')
    $previousTestExternalEffects = [Environment]::GetEnvironmentVariable('GEWU_TEST_EXTERNAL_EFFECTS', 'Process')
    $previousInstanceId = [Environment]::GetEnvironmentVariable('GEWU_INSTANCE_ID', 'Process')
    try {
        $env:GEWU_INSTANCE_ID = if ($Profile -eq 'TEST') { $testRuntimeInstanceId } else { $Profile }
        Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
        if ($devCandidateBundle) { $env:GEWU_DEV_EXTERNAL_EFFECTS = 'offline' }
        if ($Profile -eq 'TEST' -and $testRuntimePolicy -eq 'live_validation') {
            $env:GEWU_TEST_EXTERNAL_EFFECTS = 'live_validation'
        } elseif ($Profile -eq 'TEST') {
            $env:GEWU_TEST_EXTERNAL_EFFECTS = 'offline'
        } else {
            Remove-Item Env:GEWU_TEST_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue
        }
        $app = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $appDir -WindowStyle Hidden -PassThru
    } finally {
        if ($null -eq $previousDevExternalEffects) { Remove-Item Env:GEWU_DEV_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
        else { $env:GEWU_DEV_EXTERNAL_EFFECTS = $previousDevExternalEffects }
        if ($null -eq $previousTestExternalEffects) { Remove-Item Env:GEWU_TEST_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
        else { $env:GEWU_TEST_EXTERNAL_EFFECTS = $previousTestExternalEffects }
        if ($null -eq $previousInstanceId) { Remove-Item Env:GEWU_INSTANCE_ID -ErrorAction SilentlyContinue }
        else { $env:GEWU_INSTANCE_ID = $previousInstanceId }
    }
    Add-ProcessRecord $processes 'app' $app $exe '--data'
    $record = New-RunRecord $Profile $entry.url $dataPath $cachePath $buildId $manifest.commit '' $processes
    if ($devCandidateBundle) {
        $record['dev_candidate_report'] = $devCandidateBundle.report_path
        $record['dev_candidate_report_sha256'] = $devCandidateBundle.report_sha256
        $record['candidate_app'] = $devCandidateBundle.candidate_app
    }
    Save-Record $record $recordPath
    $ready = $false
    for ($i=0; $i -lt 90; $i++) { $health = Get-Health $entry.url; if ($health -and $health.application -eq 'product-tagging-workbench') { $ready = $true; break }; Start-Sleep -Milliseconds 500 }
    if (-not $ready) { throw "$Profile failed to start. Inspect its data\logs\desktop.log" }
    if ((Get-CanonicalPath $health.storage) -ne $dataPath -or $health.build_id -ne $buildId -or $health.commit -ne $manifest.commit) { throw "$Profile runtime identity does not match its manifest or data path." }
    $expectedPolicy = if ($Profile -eq 'TEST') { @($testRuntimePolicy) } else { @($devRuntimePolicy) }
    $expectedInstance = if ($Profile -eq 'TEST') { @($testRuntimeInstanceId) } else { @($devRuntimeInstanceId) }
    if ($Profile -ne 'PROD' -and -not (Test-ProfileRuntime $entry.url $Profile.ToLowerInvariant() $expectedPolicy $expectedInstance)) {
        throw "$Profile did not report the selected runtime profile, policy and instance id after startup. It remains untouched for inspection."
    }
    $runtimeOwner = Get-PortOwner $port
    $runtimeProc = if ($runtimeOwner) { Get-ProcessIdentity ([int]$runtimeOwner) } else { $null }
    $expectedRuntime = Get-CanonicalPath (Join-Path $appDir 'runtime\pythonw.exe')
    $expectedBootstrap = Join-Path $appDir 'scripts\desktop_bootstrap.py'
    if (-not $runtimeProc -or -not $runtimeProc.ExecutablePath -or (Get-CanonicalPath $runtimeProc.ExecutablePath) -ne $expectedRuntime -or
        -not $runtimeProc.CommandLine -or $runtimeProc.CommandLine -notlike "*$expectedBootstrap*") {
        throw "$Profile listener process does not match the frozen app's embedded Python bootstrap. It remains untouched for inspection."
    }
    $runtimeProcess = Get-Process -Id ([int]$runtimeOwner) -ErrorAction SilentlyContinue
    if (-not $runtimeProcess) { throw "$Profile runtime process exited before its identity could be recorded." }
    Add-ProcessRecord $processes 'app-runtime' $runtimeProcess $expectedRuntime $expectedBootstrap
}

$health = Get-Health $(if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url })
if ($Profile -eq 'DEV' -and -not (Test-ProfileRuntime "http://127.0.0.1:$($entry.apiPort)" 'dev' @($devRuntimePolicy) @($devRuntimeInstanceId))) {
    throw 'DEV API did not report profile=dev and the selected external-call policy.'
}
$runtimeStatus = Get-RuntimeStatus $(if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url })
if (-not $health -or (Get-CanonicalPath $health.storage) -ne $dataPath) { throw "$Profile health does not identify its configured data directory." }
if ($Profile -eq 'PROD' -and
    (-not $runtimeStatus -or $runtimeStatus.profile -ne 'prod' -or $runtimeStatus.instance_id -ne 'PROD' -or
     $health.build_id -ne $promotion.build_id -or $health.commit -ne $promotion.commit -or
     $health.source_sha256 -ne $promotion.source_sha256 -or
     $runtimeStatus.source.build_id -ne $promotion.build_id -or $runtimeStatus.source.commit -ne $promotion.commit -or
     $runtimeStatus.source.source_sha256 -ne $promotion.source_sha256)) {
    throw 'Live PROD runtime identity does not match the promoted build, source digest, profile, or instance. No verification record was written.'
}
$record = New-RunRecord $Profile $entry.url $dataPath $cachePath $(if ($Profile -eq 'DEV') { $health.build_id } else { $buildId }) $health.commit $(if ($Profile -eq 'DEV') { $config.projectRoot } else { '' }) $processes
if ($devCandidateBundle) {
    $record['dev_candidate_report'] = $devCandidateBundle.report_path
    $record['dev_candidate_report_sha256'] = $devCandidateBundle.report_sha256
    $record['candidate_app'] = $devCandidateBundle.candidate_app
}
if ($Profile -eq 'PROD') {
    $runtimeProcessRecord = $processes | Where-Object role -eq 'app-runtime' | Select-Object -First 1
    if (-not $runtimeProcessRecord) { throw 'Verified PROD runtime process identity is missing; no PROD verification was recorded.' }
    $verificationRoot = Split-Path -Parent (Get-CanonicalPath $entry.app)
    $verificationPath = Join-Path $verificationRoot ('PROD_VERIFICATION-' + [Guid]::NewGuid().ToString('N') + '.json')
    if (Test-Path -LiteralPath $verificationPath) { throw 'Unique PROD verification path unexpectedly exists.' }
    $verification = [ordered]@{
        schema = 1; status = 'verified_live_prod'; created_at = [DateTime]::UtcNow.ToString('o')
        profile = [string]$runtimeStatus.profile; instance_id = [string]$runtimeStatus.instance_id
        version = [string]$health.version; build_id = [string]$health.build_id; commit = [string]$health.commit
        source_sha256 = [string]$health.source_sha256; manifest_sha256 = [string]$health.manifest_sha256
        app = Get-CanonicalPath $entry.app; app_manifest_sha256 = [string]$promotion.app_manifest_sha256
        data_path = $dataPath; cache_path = $cachePath; url = [string]$entry.url
        promotion_path = $promotionPath; promotion_sha256 = (Get-FileHash -LiteralPath $promotionPath -Algorithm SHA256).Hash.ToLowerInvariant()
        run_record = $recordPath; run_record_profile = 'PROD'; process = $runtimeProcessRecord
    }
    $tempVerification = "$verificationPath.tmp"
    $verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tempVerification -Encoding UTF8
    Move-Item -LiteralPath $tempVerification -Destination $verificationPath
    $record['prod_verification_path'] = $verificationPath
    $record['prod_verification_sha256'] = (Get-FileHash -LiteralPath $verificationPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
Save-Record $record $recordPath
Write-Output "$Profile ready: $($entry.url)"
Write-Output "  data: $dataPath"
Write-Output "  cache: $cachePath"
Write-Output "  build: $($record.build_id) / $($record.commit)"
if (-not $NoOpen) { Open-RunCenterPage $entry.url | Out-Null; Show-ProdPendingNotice $health }
