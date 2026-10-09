[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('DEV', 'TEST', 'PROD')][string]$Profile,
    [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'GeWuWorkbench\RunCenter\config.json'),
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
            $home = ($homeLine -replace '^\s*home\s*=\s*', '').Trim()
            $basePython = Join-Path $home 'python.exe'
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
function Test-ProfileRuntime([string]$Url, [string]$ExpectedProfile) {
    $status = Get-RuntimeStatus $Url
    if (-not $status -or $status.profile -ne $ExpectedProfile) { return $false }
    if ($ExpectedProfile -ne 'prod' -and $status.external_effects.policy -ne 'offline') { return $false }
    return $true
}
function Show-ProdPendingNotice($Health) {
    if ($Profile -ne 'PROD' -or $Health.release_status -ne 'pending_acceptance') { return }
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
$runRoot = Join-Path $runCenterRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$recordPath = Join-Path $runRoot "$Profile.json"
$record = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }

if ($Action -eq 'Status') {
    $url = $entry.url
    $healthUrl = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $url }
    $health = Get-Health $healthUrl
    $runtime = Get-RuntimeStatus $healthUrl
    $runtimeIdentity = if ($runtime) { "$($runtime.profile)/$($runtime.external_effects.policy)" } else { 'runtime-status-unavailable' }
    [pscustomobject]@{ profile = $Profile; url = $url; api_url = $healthUrl; page_status = if ($Profile -eq 'DEV') { if (Get-PortOwner ([int]$entry.webPort)) { 'listening' } else { 'stopped' } } else { if ($health) { 'ready' } else { 'stopped' } }; api_status = if ($health) { $health.status } else { 'stopped' }; runtime = $runtimeIdentity; build_id = $health.build_id; commit = $health.commit; storage = $health.storage; record = $recordPath } | Format-List
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

if ($Action -eq 'Start' -and $record) {
    $healthUrl = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url }
    $runningHealth = Get-Health $healthUrl
    if ($Profile -eq 'DEV' -and $runningHealth -and $runningHealth.application -eq 'product-tagging-workbench' -and
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
    if ($recordMatches -and $Profile -ne 'PROD') { $recordMatches = Test-ProfileRuntime $healthUrl $Profile.ToLowerInvariant() }
    if ($recordMatches) {
        $servicePort = if ($Profile -eq 'DEV') { [int]$entry.apiPort } else { [int]$entry.port }
        $serviceOwner = Get-PortOwner $servicePort
        if (-not $serviceOwner -or $serviceOwner -notin @($record.processes | ForEach-Object { [int]$_.pid })) {
            throw "$Profile is responding, but the listener PID is not part of its recorded process identity. It was left running."
        }
        foreach ($processRole in $record.processes.role | Select-Object -Unique) {
            $null = Test-RecordedProcess $record $processRole
        }
        if ($Profile -eq 'DEV') {
            $webOwner = Get-PortOwner ([int]$entry.webPort)
            if (-not $webOwner -or $webOwner -notin @($record.processes | Where-Object role -eq 'web' | ForEach-Object { [int]$_.pid })) {
                throw 'DEV API is running but the recorded Vite listener is unavailable or no longer matches. Stop the owned entry before restarting.'
            }
        }
        if (-not $NoOpen) { Start-Process $entry.url; Show-ProdPendingNotice $runningHealth }
        Write-Output "$Profile is already running with the recorded identity: $($record.build_id) / $($record.commit)"
        exit 0
    }
}

if ($Action -eq 'Stop') {
    if (-not $record) { Write-Output "No launcher-owned $Profile instance is recorded."; exit 0 }
    $url = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($entry.apiPort)" } else { $entry.url }
    $servicePort = if ($Profile -eq 'DEV') { [int]$entry.apiPort } else { [int]$entry.port }
    # Verify every surviving recorded PID before sending a stop request. A matching health URL
    # alone is insufficient: another process could have taken over the port/data directory.
    foreach ($role in $record.processes.role | Select-Object -Unique) { $null = Test-RecordedProcess $record $role }
    $serviceOwner = Get-PortOwner $servicePort
    if ($serviceOwner) {
        $ownerRecord = $record.processes | Where-Object { [int]$_.pid -eq [int]$serviceOwner } | Select-Object -First 1
        if (-not $ownerRecord) { throw "Port $servicePort is owned by PID $serviceOwner, which is not recorded for this $Profile instance. Nothing was stopped." }
        $null = Test-RecordedProcess $record $ownerRecord.role
        $owner = Get-ProcessIdentity ([int]$serviceOwner)
        if (-not $owner -or -not $owner.ExecutablePath) { throw "Cannot verify the listener process for port $servicePort. Nothing was stopped." }
    }
    $health = Get-Health $url
    if ($serviceOwner -and -not $health) { throw "Port $servicePort is owned by a recorded process but does not return its health identity. Nothing was stopped." }
    if (-not $serviceOwner -and $health) { throw "Health responded on $url without a matching listener snapshot. Nothing was stopped." }
    if ($health -and ($health.application -ne 'product-tagging-workbench' -or
        (Get-CanonicalPath $health.storage) -ne (Get-CanonicalPath $record.data) -or
        $health.build_id -ne $record.build_id -or $health.commit -ne $record.commit)) {
        throw "Service identity on $url does not match the recorded $Profile process, build, and data path. Nothing was stopped."
    }
    if ($health) {
        if ($Profile -ne 'PROD' -and -not (Test-ProfileRuntime $url $Profile.ToLowerInvariant())) {
            throw "Runtime profile does not match this $Profile entry, or it is not explicitly offline. Nothing was stopped."
        }
        try { Invoke-RestMethod -Method Post -Uri "$url/api/server/stop" -TimeoutSec 3 | Out-Null } catch { }
        $released = $false
        for ($i=0; $i -lt 60; $i++) {
            if (-not (Get-PortOwner $servicePort)) { $released = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $released) { throw "The recorded $Profile service did not release port $servicePort after its graceful stop request. Its process record was retained." }
    }
    if ($Profile -eq 'DEV') {
        $webOwner = Get-PortOwner ([int]$entry.webPort)
        if ($webOwner) {
            $webRecord = $record.processes | Where-Object { $_.role -eq 'web' -and [int]$_.pid -eq [int]$webOwner } | Select-Object -First 1
            if (-not $webRecord) { throw "DEV web port $($entry.webPort) is now owned by an unrecorded process. The process record was retained." }
            $webProc = Test-RecordedProcess $record 'web'
            if (-not $webProc) { throw 'The recorded Vite process is no longer available; the port remains occupied.' }
            Stop-Process -Id ([int]$webProc.ProcessId) -ErrorAction Stop
            for ($i=0; $i -lt 20; $i++) { if (-not (Get-PortOwner ([int]$entry.webPort))) { break }; Start-Sleep -Milliseconds 250 }
            if (Get-PortOwner ([int]$entry.webPort)) { throw "DEV web port $($entry.webPort) remains occupied. The process record was retained." }
        }
    }
    Remove-Item -LiteralPath $recordPath -Force
    Write-Output "Stopped only matching launcher-recorded $Profile processes."
    exit 0
}

$dataPath = Get-CanonicalPath $entry.data
$cachePath = Get-CanonicalPath $entry.cache
if ($Profile -eq 'PROD') {
    if ((Get-CanonicalPath $entry.app) -ne (Get-CanonicalPath 'K:\Bobbytagging\Candidate-beta.8\app') -or
        $dataPath -ne (Get-CanonicalPath 'K:\Bobbytagging\Data') -or
        $cachePath -ne (Get-CanonicalPath 'K:\Bobbytagging\Cache')) {
        throw 'PROD is pinned to the beta.8 candidate app and existing formal Data/Cache paths. Refusing a changed path.'
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
if ($Profile -ne 'PROD') { $env:GEWU_ALLOW_EXTERNAL_EFFECTS = '0' } else { Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
if ($Profile -eq 'DEV') {
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
    $env:GEWU_REPORTS_DIR = $config.profiles.TEST.reports
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    $apiArgs = @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$apiPort",'--reload')
    $api = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'api.out.log') -RedirectStandardError (Join-Path $logRoot 'api.err.log') -PassThru
    Add-ProcessRecord $processes 'api' $api $python "--port $apiPort"
    $record = New-RunRecord $Profile "http://127.0.0.1:$webPort" $dataPath $cachePath '' '' $root $processes
    Save-Record $record $recordPath
    $env:GEWU_DEV_API_URL = "http://127.0.0.1:$apiPort"
    $web = Start-Process -FilePath $node -ArgumentList @($vite,'--host','127.0.0.1','--port',"$webPort") -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'web.out.log') -RedirectStandardError (Join-Path $logRoot 'web.err.log') -PassThru
    Add-ProcessRecord $processes 'web' $web $node 'vite.js'
    $record = New-RunRecord $Profile "http://127.0.0.1:$webPort" $dataPath $cachePath '' '' $root $processes
    Save-Record $record $recordPath
    $ready = $false
    for ($i=0; $i -lt 90; $i++) { if ((Get-Health "http://127.0.0.1:$apiPort") -and (Get-PortOwner $webPort)) { $ready = $true; break }; Start-Sleep -Milliseconds 500 }
    if (-not $ready) { throw "DEV did not become ready. Logs: $logRoot" }
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
    $appDir = Get-CanonicalPath $entry.app
    $exe = Join-Path $appDir 'Workbench.exe'
    $manifestPath = Join-Path $appDir 'build-manifest.json'
    if (-not (Test-Path -LiteralPath $exe) -or -not (Test-Path -LiteralPath $manifestPath)) { throw "$Profile frozen app or build manifest is missing: $appDir" }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $buildId = $manifest.build_id
    if (-not $buildId) { throw "Build manifest has no build_id: $manifestPath" }
    Test-AppManifestFiles $appDir $manifest
    $port = [int]$entry.port
    if ($Profile -eq 'TEST') {
        if ($manifest.dirty -ne $false -or $manifest.build_mode -ne 'dev' -or -not $manifest.commit) {
            throw 'TEST requires a clean, commit-pinned development build manifest.'
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
        if ($Profile -ne 'PROD' -and -not (Test-ProfileRuntime $entry.url $Profile.ToLowerInvariant())) {
            throw "$Profile port is serving a build without the required offline runtime identity. Nothing was stopped or adopted."
        }
        Write-Output "$Profile is already serving the exact frozen build and data directory, but was not launched by this Run Center. Its process is not claimed for Stop."
        if (-not $NoOpen) { Start-Process $entry.url; Show-ProdPendingNotice $health }
        exit 0
    }
    $env:GEWU_PROFILE = $Profile.ToLowerInvariant()
    $env:TAG_PROVIDER_HOME = if ($entry.providerHome) { [IO.Path]::GetFullPath([string]$entry.providerHome) } else { Join-Path $dataPath 'providers' }
    $env:GEWU_API_URL = $entry.url
    $env:GEWU_FRONTEND_URL = $entry.url
    $env:GEWU_INSTANCE_ID = $Profile
    $env:GEWU_REPORTS_DIR = $config.profiles.TEST.reports
    if ($Profile -ne 'PROD') { $env:GEWU_ALLOW_EXTERNAL_EFFECTS = '0' } else { Remove-Item Env:GEWU_ALLOW_EXTERNAL_EFFECTS -ErrorAction SilentlyContinue }
    $args = @('--data',$dataPath,'--cache',$cachePath,'--port',"$port",'--no-open')
    $app = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $appDir -WindowStyle Hidden -PassThru
    Add-ProcessRecord $processes 'app' $app $exe '--data'
    $record = New-RunRecord $Profile $entry.url $dataPath $cachePath $buildId $manifest.commit '' $processes
    Save-Record $record $recordPath
    $ready = $false
    for ($i=0; $i -lt 90; $i++) { $health = Get-Health $entry.url; if ($health -and $health.application -eq 'product-tagging-workbench') { $ready = $true; break }; Start-Sleep -Milliseconds 500 }
    if (-not $ready) { throw "$Profile failed to start. Inspect its data\logs\desktop.log" }
    if ((Get-CanonicalPath $health.storage) -ne $dataPath -or $health.build_id -ne $buildId -or $health.commit -ne $manifest.commit) { throw "$Profile runtime identity does not match its manifest or data path." }
    if ($Profile -ne 'PROD' -and -not (Test-ProfileRuntime $entry.url $Profile.ToLowerInvariant())) {
        throw "$Profile did not report the expected offline runtime profile after startup. It remains untouched for inspection."
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
if ($Profile -eq 'DEV' -and -not (Test-ProfileRuntime "http://127.0.0.1:$($entry.apiPort)" 'dev')) {
    throw 'DEV API did not report profile=dev and offline external-call policy.'
}
$record = New-RunRecord $Profile $entry.url $dataPath $cachePath $(if ($Profile -eq 'DEV') { $health.build_id } else { $buildId }) $health.commit $(if ($Profile -eq 'DEV') { $config.projectRoot } else { '' }) $processes
Save-Record $record $recordPath
Write-Output "$Profile ready: $($entry.url)"
Write-Output "  data: $dataPath"
Write-Output "  cache: $cachePath"
Write-Output "  build: $($record.build_id) / $($record.commit)"
if (-not $NoOpen) { Start-Process $entry.url; Show-ProdPendingNotice $health }
