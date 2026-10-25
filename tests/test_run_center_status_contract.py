"""Read-only fixtures for the control panel's Run Center status states."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="PowerShell is required")


def test_status_distinguishes_verified_stopped_degraded_and_conflict() -> None:
    module = (ROOT / "scripts" / "run-center-status.psm1").as_posix()
    command = rf"""
Import-Module '{module}' -Force -ErrorAction Stop
$entry = [pscustomobject]@{{url='http://127.0.0.1:5174';apiPort=8896;webPort=5174;data='K:\\DevData'}}
$record = [pscustomobject]@{{data='K:\\DevData';build_id='build';commit='commit';dev_candidate_report='K:\\Reports\\candidate.json';processes=@(
  [pscustomobject]@{{role='app';pid=101}}, [pscustomobject]@{{role='app-runtime';pid=102}})}}
$health = [pscustomobject]@{{application='product-tagging-workbench';storage='K:\\DevData';build_id='build';commit='commit'}}
$runtime = [pscustomobject]@{{profile='dev';instance_id='DEV';external_effects=[pscustomobject]@{{policy='offline'}}}}
$verify = {{ param($r,$role) if($role -eq 'app'){{return $null}}; return [pscustomobject]@{{pid=102}} }}
$stopped = Get-DgControlPanelStatus -Profile DEV -Entry $entry -VerifyProcess $verify
$foreign = Get-DgControlPanelStatus -Profile DEV -Entry $entry -ServiceOwner 900 -VerifyProcess $verify
$stale = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -VerifyProcess $verify
$healthy = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -ServiceOwner 102 -Health $health -Runtime $runtime -VerifyProcess $verify
$noHealth = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -ServiceOwner 102 -Runtime $runtime -VerifyProcess $verify
$badHealth = [pscustomobject]@{{application='product-tagging-workbench';storage='K:\\Other';build_id='build';commit='commit'}}
$mismatch = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -ServiceOwner 102 -Health $badHealth -Runtime $runtime -VerifyProcess $verify
$badRuntime = [pscustomobject]@{{profile='dev';instance_id='DEV';external_effects=[pscustomobject]@{{policy='live_validation'}}}}
$badPolicy = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -ServiceOwner 102 -Health $health -Runtime $badRuntime -VerifyProcess $verify
$missingListener = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $record -ServiceOwner 102 -Health $health -Runtime $runtime -VerifyProcess {{param($r,$role) return $null}}
$liveRecord = [pscustomobject]@{{data='K:\DevData';build_id='build';commit='commit';processes=@(
  [pscustomobject]@{{role='api';pid=201}}, [pscustomobject]@{{role='web';pid=202}})}}
$liveRuntime = [pscustomobject]@{{profile='dev';instance_id='DEV';external_effects=[pscustomobject]@{{policy='live_validation'}}}}
$webAsApi = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $liveRecord -ServiceOwner 202 -WebOwner 202 -Health $health -Runtime $liveRuntime -VerifyProcess {{param($r,$role) return [pscustomobject]@{{pid=201}}}}
$missingWeb = Get-DgControlPanelStatus -Profile DEV -Entry $entry -Record $liveRecord -ServiceOwner 201 -WebOwner 202 -Health $health -Runtime $liveRuntime -VerifyProcess {{param($r,$role) if($role -eq 'web'){{return $null}}; return [pscustomobject]@{{pid=201}}}}
$prodEntry = [pscustomobject]@{{url='http://127.0.0.1:8765';port=8765;data='K:\DevData'}}
$prodRuntime = [pscustomobject]@{{profile='prod';instance_id='PROD';external_effects=[pscustomobject]@{{policy='online'}}}}
$prod = Get-DgControlPanelStatus -Profile PROD -Entry $prodEntry -Record $record -ServiceOwner 102 -Health $health -Runtime $prodRuntime -VerifyProcess $verify
@($stopped,$foreign,$stale,$healthy,$noHealth,$mismatch,$badPolicy,$missingListener,$webAsApi,$missingWeb,$prod) | ConvertTo-Json -Depth 5 -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    states = json.loads(result.stdout)
    assert [(item["state"], item["reason"]) for item in states] == [
        ("stopped", "no_record_or_listener"),
        ("identity_conflict", "unrecorded_listener"),
        ("degraded", "record_without_listener"),
        ("verified_running", "ok"),
        ("degraded", "health_unavailable"),
        ("identity_conflict", "health_identity_mismatch"),
        ("identity_conflict", "runtime_identity_mismatch"),
        ("degraded", "recorded_listener_missing"),
        ("identity_conflict", "unrecorded_api_owner"),
        ("degraded", "recorded_listener_missing"),
        ("verified_running", "ok"),
    ]
    assert states[3]["ownership_verified"] is True
    assert all(item["ownership_verified"] is False for i, item in enumerate(states) if i not in (3, 10))
    assert states[3]["mode"] == "candidate"
    assert states[3]["url"] == "http://127.0.0.1:8896"
