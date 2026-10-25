function Get-DgControlPanelStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('DEV', 'PROD', 'TEST')][string]$Profile,
        [Parameter(Mandatory)]$Entry,
        $Record,
        $ServiceOwner,
        $WebOwner,
        $Health,
        $Runtime,
        [Parameter(Mandatory)][scriptblock]$VerifyProcess
    )
    $apiUrl = if ($Profile -eq 'DEV') { "http://127.0.0.1:$($Entry.apiPort)" } else { [string]$Entry.url }
    $mode = if ($Profile -eq 'DEV') { if ($Record -and $Record.dev_candidate_report) { 'candidate' } else { 'live' } } else { 'prod' }
    $pageUrl = if ($mode -eq 'candidate') { $apiUrl } else { [string]$Entry.url }
    $state = 'stopped'
    $reason = 'no_record_or_listener'
    if (-not $Record) {
        if ($ServiceOwner -or $WebOwner) { $state = 'identity_conflict'; $reason = 'unrecorded_listener' }
    } elseif (-not $ServiceOwner) {
        $state = if ($WebOwner) { 'identity_conflict' } else { 'degraded' }
        $reason = if ($WebOwner) { 'web_without_recorded_api' } else { 'record_without_listener' }
    } else {
        $apiRoles = if ($mode -eq 'live') { @('api', 'api-runtime') } else { @('app', 'app-runtime') }
        $knownApiPids = @($Record.processes | Where-Object { $_.role -in $apiRoles } | ForEach-Object { [int]$_.pid })
        if ($ServiceOwner -notin $knownApiPids) {
            $state = 'identity_conflict'; $reason = 'unrecorded_api_owner'
        } elseif ($Profile -eq 'DEV' -and $mode -eq 'live' -and
            (-not $WebOwner -or $WebOwner -notin @($Record.processes | Where-Object role -eq 'web' | ForEach-Object { [int]$_.pid }))) {
            $state = if ($WebOwner) { 'identity_conflict' } else { 'degraded' }
            $reason = if ($WebOwner) { 'unrecorded_web_owner' } else { 'web_unavailable' }
        } else {
            $state = 'verified_running'; $reason = 'ok'
            foreach ($role in @($Record.processes.role | Select-Object -Unique)) {
                try {
                    $verifiedProcess = & $VerifyProcess $Record $role
                    # A packaged launcher parent may exit while its recorded runtime child stays healthy.
                    $roleRecord = $Record.processes | Where-Object role -eq $role | Select-Object -First 1
                    if (-not $verifiedProcess -and [int]$roleRecord.pid -in @([int]$ServiceOwner, [int]$WebOwner)) {
                        $state = 'degraded'; $reason = 'recorded_listener_missing'; break
                    }
                } catch {
                    $state = 'identity_conflict'; $reason = 'recorded_process_mismatch'; break
                }
            }
            if ($state -eq 'verified_running' -and -not $Health) {
                $state = 'degraded'; $reason = 'health_unavailable'
            }
            if ($state -eq 'verified_running') {
                $storageMatches = $false
                try {
                    $storageMatches = $Health.storage -and $Record.data -and
                        [IO.Path]::GetFullPath([string]$Health.storage).TrimEnd('\') -ieq
                        [IO.Path]::GetFullPath([string]$Record.data).TrimEnd('\')
                } catch { }
                if ($Health.application -ne 'product-tagging-workbench' -or -not $storageMatches -or
                    $Health.build_id -ne $Record.build_id -or $Health.commit -ne $Record.commit) {
                    $state = 'identity_conflict'; $reason = 'health_identity_mismatch'
                }
            }
            if ($state -eq 'verified_running' -and -not $Runtime) {
                $state = 'degraded'; $reason = 'runtime_unavailable'
            }
            if ($state -eq 'verified_running') {
                $expectedPolicy = if ($mode -eq 'candidate') { @('offline') } elseif ($Profile -eq 'PROD') { @('online') } else { @('offline', 'live_validation') }
                if ($Runtime.profile -ne $Profile.ToLowerInvariant() -or $Runtime.instance_id -ne $Profile -or
                    $Runtime.external_effects.policy -notin $expectedPolicy) {
                    $state = 'identity_conflict'; $reason = 'runtime_identity_mismatch'
                }
            }
        }
    }
    [ordered]@{
        schema = 1; profile = $Profile; state = $state; reason = $reason
        ownership_verified = ($state -eq 'verified_running'); mode = $mode
        policy = if ($Runtime) { [string]$Runtime.external_effects.policy } else { $null }
        instance_id = if ($Runtime) { [string]$Runtime.instance_id } else { $null }
        url = $pageUrl; api_url = $apiUrl
        build_id = if ($Health) { [string]$Health.build_id } elseif ($Record) { [string]$Record.build_id } else { $null }
        commit = if ($Health) { [string]$Health.commit } elseif ($Record) { [string]$Record.commit } else { $null }
        storage = if ($Health) { [string]$Health.storage } else { [string]$Entry.data }
        candidate_report = if ($Record -and $Record.dev_candidate_report) { [string]$Record.dev_candidate_report } else { $null }
    }
}
Export-ModuleMember -Function Get-DgControlPanelStatus
