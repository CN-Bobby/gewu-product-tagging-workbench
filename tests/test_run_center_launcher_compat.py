"""Regression checks for already installed desktop entry compatibility."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="PowerShell is required")
LEGACY_COMMIT = "80fa8c41618c7eadff7bdfd90aba6fb7be687015"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _run(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT, capture_output=True, text=True, check=False, env=env,
    )


def test_existing_beta13_prod_promotion_can_restart_without_moved_zip(tmp_path: Path) -> None:
    app = tmp_path / "Candidate-beta.13" / "app"
    app.mkdir(parents=True)
    manifest = app / "build-manifest.json"
    build = "0.5.0-beta.13+80fa8c41618c"
    _write(manifest, {
        "version": "0.5.0-beta.13", "build_id": build,
        "commit": LEGACY_COMMIT, "source_sha256": "a" * 64,
    })
    candidate = tmp_path / "candidate.json"
    _write(candidate, {
        "status": "promoted", "profile": "dev", "version": "0.5.0-beta.13",
        "build_id": build, "commit": LEGACY_COMMIT,
        "manifest_sha256": _hash(manifest), "portable_sha256": "b" * 64,
        "portable_sidecar_sha256": "d" * 64,
    })
    manual = tmp_path / "manual.json"
    _write(manual, {
        "status": "passed", "profile": "dev", "version": "0.5.0-beta.13",
        "build_id": build, "commit": LEGACY_COMMIT,
        "candidate_report_sha256": _hash(candidate),
        "app_manifest_sha256": _hash(manifest), "portable_sha256": "b" * 64,
        "portable_sidecar_sha256": "d" * 64,
    })
    config = tmp_path / "config.json"
    _write(config, {})
    data = tmp_path / "Data"
    cache = tmp_path / "Cache"
    data.mkdir()
    cache.mkdir()
    promotion = app.parent / "PROMOTION.json"
    base = {
        "schema": 1, "status": "promoted", "version": "0.5.0-beta.13",
        "build_id": build, "commit": LEGACY_COMMIT,
        "target_app": str(app), "run_center_config": str(config),
        "prod_data_path": str(data), "prod_cache_path": str(cache),
        "manual_result": str(manual), "manual_result_sha256": _hash(manual),
        "candidate_report": str(candidate), "candidate_report_sha256": _hash(candidate),
        "app_manifest_sha256": _hash(manifest),
        "portable_zip": str(tmp_path / "moved.zip"), "portable_sha256": "b" * 64,
        "portable_sidecar_sha256": "d" * 64,
    }
    _write(promotion, base)
    module = ROOT / "scripts" / "dev-release-gates.psm1"
    command = (
        f"$ErrorActionPreference='Stop'; Import-Module '{module}' -Force; "
        f"$r=Test-DgLegacyProdPromotion '{promotion}' '{app}' '{config}' '{data}' '{cache}'; "
        "Write-Output $r.source_sha256"
    )
    accepted = _run(command)
    assert accepted.returncode == 0, accepted.stderr
    assert "a" * 64 in accepted.stdout

    base["commit"] = "c" * 40
    _write(promotion, base)
    rejected = _run(command)
    assert rejected.returncode != 0

    base["commit"] = LEGACY_COMMIT
    base["version"] = "0.5.0-beta.15"
    _write(promotion, base)
    rejected_new_version = _run(command)
    assert rejected_new_version.returncode != 0


def test_dev_shortcut_does_not_replace_a_running_offline_candidate() -> None:
    runner = (ROOT / "scripts" / "run-center.ps1").read_text(encoding="utf-8-sig")
    assert "runningDevCandidateBundle" in runner
    assert "Test-DgCandidateBundle" in runner
    assert "@('offline')" in runner
    assert "frozen DEV candidate" in runner


def test_release_module_uses_windows_utility_when_module_path_is_shadowed(tmp_path: Path) -> None:
    shadow = tmp_path / "Microsoft.PowerShell.Utility"
    shadow.mkdir()
    (shadow / "Microsoft.PowerShell.Utility.psd1").write_text(
        "@{ModuleVersion='7.0.0'; GUID='6d887eed-5268-4e6a-9ad6-350471c3aef7'; FunctionsToExport=@()}",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PSModulePath"] = str(tmp_path) + os.pathsep + env.get("PSModulePath", "")
    module = ROOT / "scripts" / "dev-release-gates.psm1"
    script = f"$ErrorActionPreference='Stop'; Import-Module '{module}' -Force; Get-Command Get-FileHash -ErrorAction Stop | Select-Object -ExpandProperty Name"
    result = _run(script, env)
    assert result.returncode == 0, result.stderr
    assert "Get-FileHash" in result.stdout
    runner = (ROOT / "scripts" / "run-center.ps1").read_text(encoding="utf-8-sig")
    assert "Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1'" in runner
