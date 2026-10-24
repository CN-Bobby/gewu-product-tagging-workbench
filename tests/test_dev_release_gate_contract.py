"""Isolated contracts for the DEV-only portable release gates."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="PowerShell is required for the Windows release-gate module")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def make_candidate(tmp_path: Path) -> dict[str, object]:
    reports = tmp_path / "reports"
    builds = tmp_path / "builds"
    app = builds / "0.5.0-beta.14-candidate.0123456789ab" / "app"
    (app / "backend").mkdir(parents=True)
    reports.mkdir()
    source = app / "backend" / "app.py"
    source.write_text("# frozen candidate\n", encoding="utf-8")
    commit = "0123456789ab" + "c" * 28
    source_sha = "a" * 64
    manifest = {
        "version": "0.5.0-beta.14",
        "build_id": "0.5.0-beta.14+0123456789ab",
        "commit": commit,
        "source_sha256": source_sha,
        "dirty": False,
        "build_mode": "candidate",
        "storage_mode": "user-selected",
        "release_status": "pending_acceptance",
        "files": {"backend/app.py": sha256(source)},
    }
    manifest_path = app / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    portable_zip = app.parent / "GeWuWorkbench-0.5.0-beta.14-portable.zip"
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(source, "backend/app.py")
        archive.write(manifest_path, "build-manifest.json")
    portable_hash = sha256(portable_zip)
    sidecar = portable_zip.with_name(portable_zip.name + ".sha256")
    sidecar.write_text(f"{portable_hash}  {portable_zip.name}\n", encoding="utf-8")
    offline_report = reports / "offline-20261024-0123456789ab.json"
    offline_report.write_text(
        json.dumps(
            {
                "status": "passed",
                "profile": "test",
                "commit": commit,
                "dirty": False,
                "external_requests": 0,
                "report_scope": "offline backend and frontend checks",
            }
        ),
        encoding="utf-8",
    )
    candidate_report = reports / "candidate-0.5.0-beta.14+0123456789ab.json"
    candidate = {
        "schema": 1,
        "status": "built_pending_dev_acceptance",
        "profile": "dev",
        "version": manifest["version"],
        "build_id": manifest["build_id"],
        "commit": commit,
        "dirty": False,
        "source_sha256": source_sha,
        "candidate_app": str(app),
        "manifest_sha256": sha256(manifest_path),
        "build_manifest_sha256": sha256(manifest_path),
        "portable_zip": str(portable_zip),
        "portable_sha256": portable_hash,
        "portable_sidecar": str(sidecar),
        "portable_sidecar_sha256": sha256(sidecar),
        "automated_report": str(offline_report),
        "automated_report_sha256": sha256(offline_report),
    }
    candidate_report.write_text(json.dumps(candidate), encoding="utf-8")
    return {
        "reports": reports,
        "builds": builds,
        "app": app,
        "candidate_report": candidate_report,
        "offline_report": offline_report,
        "manifest": manifest_path,
        "portable_zip": portable_zip,
        "sidecar": sidecar,
        "commit": commit,
        "source_sha": source_sha,
    }


def invoke_gate(tmp_path: Path, function: str, args: str) -> subprocess.CompletedProcess[str]:
    module = ROOT / "scripts" / "dev-release-gates.psm1"
    return powershell(
        "$ErrorActionPreference='Stop'; "
        f"Import-Module -Force '{module}'; "
        f"{function} {args} | ConvertTo-Json -Depth 8"
    )


def test_candidate_contract_accepts_exact_dev_bundle_and_offline_report(tmp_path: Path) -> None:
    files = make_candidate(tmp_path)
    result = invoke_gate(
        tmp_path,
        "Test-DgCandidateBundle",
        f"-ReportPath '{files['candidate_report']}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}'",
    )
    assert result.returncode == 0, result.stderr
    identity = json.loads(result.stdout)
    assert identity["profile"] == "dev"
    assert identity["build_id"] == "0.5.0-beta.14+0123456789ab"
    assert identity["portable_sha256"] == sha256(files["portable_zip"])


def test_candidate_contract_rejects_test_runtime_and_tampered_zip(tmp_path: Path) -> None:
    files = make_candidate(tmp_path)
    report = json.loads(files["candidate_report"].read_text(encoding="utf-8"))
    report["profile"] = "test"
    files["candidate_report"].write_text(json.dumps(report), encoding="utf-8")
    rejected_profile = invoke_gate(
        tmp_path,
        "Test-DgCandidateBundle",
        f"-ReportPath '{files['candidate_report']}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}'",
    )
    assert rejected_profile.returncode != 0
    assert "clean DEV build awaiting manual acceptance" in rejected_profile.stderr

    report["profile"] = "dev"
    files["candidate_report"].write_text(json.dumps(report), encoding="utf-8")
    with files["portable_zip"].open("ab") as stream:
        stream.write(b"tampered")
    rejected_hash = invoke_gate(
        tmp_path,
        "Test-DgCandidateBundle",
        f"-ReportPath '{files['candidate_report']}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}'",
    )
    assert rejected_hash.returncode != 0
    assert "SHA-256 changed" in rejected_hash.stderr


def test_manual_contract_binds_dev_runtime_and_all_acceptance_hashes(tmp_path: Path) -> None:
    files = make_candidate(tmp_path)
    checklist = files["reports"] / "manual-checklist.md"
    checklist.write_text("- [x] Human checked the DEV candidate\n", encoding="utf-8")
    result_path = files["reports"] / "manual-result.json"
    run_record_path = files["reports"] / "run-center-dev.json"
    run_record = {
        "profile": "DEV",
        "dev_candidate_report": str(files["candidate_report"]),
        "dev_candidate_report_sha256": sha256(files["candidate_report"]),
        "build_id": "0.5.0-beta.14+0123456789ab",
        "commit": files["commit"],
        "data": str(tmp_path / "dev-data"),
    }
    run_record_path.write_text(json.dumps(run_record), encoding="utf-8")
    manual = {
        "schema": 1,
        "status": "passed",
        "profile": "dev",
        "runtime_policy": "offline",
        "runtime_instance_id": "DEV",
        "version": "0.5.0-beta.14",
        "build_id": "0.5.0-beta.14+0123456789ab",
        "commit": files["commit"],
        "dirty": False,
        "source_sha256": files["source_sha"],
        "data_path": str(tmp_path / "dev-data"),
        "candidate_report": str(files["candidate_report"]),
        "candidate_report_sha256": sha256(files["candidate_report"]),
        "candidate_app": str(files["app"]),
        "app_manifest_sha256": sha256(files["manifest"]),
        "portable_zip": str(files["portable_zip"]),
        "portable_sha256": sha256(files["portable_zip"]),
        "portable_sidecar": str(files["sidecar"]),
        "portable_sidecar_sha256": sha256(files["sidecar"]),
        "automated_report": str(files["offline_report"]),
        "automated_report_sha256": sha256(files["offline_report"]),
        "manual_checklist": str(checklist),
        "manual_checklist_sha256": sha256(checklist),
        "run_id": "run-123",
        "human_tested_at": "2026-10-24T00:00:00Z",
        "run_center_record": str(run_record_path),
        "run_center_record_sha256": sha256(run_record_path),
        "checklist_items": [{"passed": True, "label": "Human checked the DEV candidate"}],
    }
    result_path.write_text(json.dumps(manual), encoding="utf-8")
    valid = invoke_gate(
        tmp_path,
        "Test-DgManualAcceptance",
        f"-ResultPath '{result_path}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}' -ExpectedDevData '{manual['data_path']}'",
    )
    assert valid.returncode == 0, valid.stderr

    manual["profile"] = "test"
    result_path.write_text(json.dumps(manual), encoding="utf-8")
    rejected_profile = invoke_gate(
        tmp_path,
        "Test-DgManualAcceptance",
        f"-ResultPath '{result_path}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}' -ExpectedDevData '{manual['data_path']}'",
    )
    assert rejected_profile.returncode != 0
    assert "must identify a passed DEV acceptance" in rejected_profile.stderr

    manual["profile"] = "dev"
    result_path.write_text(json.dumps(manual), encoding="utf-8")
    run_record = json.loads(run_record_path.read_text(encoding="utf-8"))
    run_record["commit"] = "f" * 40
    run_record_path.write_text(json.dumps(run_record), encoding="utf-8")
    rejected_record = invoke_gate(
        tmp_path,
        "Test-DgManualAcceptance",
        f"-ResultPath '{result_path}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}' -ExpectedDevData '{manual['data_path']}'",
    )
    assert rejected_record.returncode != 0
    assert "record hash changed" in rejected_record.stderr


def test_candidate_contract_rejects_app_outside_builds_root(tmp_path: Path) -> None:
    files = make_candidate(tmp_path)
    outside = tmp_path / "outside" / "app"
    shutil.copytree(files["app"], outside)
    report = json.loads(files["candidate_report"].read_text(encoding="utf-8"))
    report["candidate_app"] = str(outside)
    files["candidate_report"].write_text(json.dumps(report), encoding="utf-8")
    result = invoke_gate(
        tmp_path,
        "Test-DgCandidateBundle",
        f"-ReportPath '{files['candidate_report']}' -ReportsRoot '{files['reports']}' -BuildsRoot '{files['builds']}'",
    )
    assert result.returncode != 0
    assert "outside its approved root" in result.stderr


def test_portable_zip_extracts_only_verified_files_without_overwrite(tmp_path: Path) -> None:
    files = make_candidate(tmp_path)
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    destination = tmp_path / "promotion-stage" / "app"
    destination.mkdir(parents=True)
    module = (ROOT / "scripts/dev-release-gates.psm1").as_posix()
    command = (
        f"Import-Module '{module}' -Force; "
        f"$m=Get-Content -LiteralPath '{files['manifest']}' -Raw | ConvertFrom-Json; "
        f"Expand-DgPortableZip '{files['portable_zip']}' '{destination}' $m '{sha256(files['manifest'])}' 'test bundle'"
    )
    extracted = powershell(command)
    assert extracted.returncode == 0, extracted.stderr
    assert (destination / "backend/app.py").read_bytes() == (files["app"] / "backend/app.py").read_bytes()
    refused = powershell(command)
    assert refused.returncode != 0
    assert "overwrite an existing file" in refused.stderr


def test_release_powershell_scripts_parse_without_running(tmp_path: Path) -> None:
    scripts = [
        "scripts/dev-release-gates.psm1",
        "scripts/new-dev-candidate.ps1",
        "scripts/new-dev-manual-acceptance.ps1",
        "scripts/finalize-dev-manual-acceptance.ps1",
        "scripts/promote-dev-candidate.ps1",
        "scripts/publish-dev-release.ps1",
        "scripts/run-center.ps1",
        "scripts/install-run-center.ps1",
    ]
    ps_files = ",".join("'" + str((ROOT / name).as_posix()).replace("'", "''") + "'" for name in scripts)
    command = (
        f"$files=@({ps_files}); $errors=@(); foreach($file in $files) "
        "{ $tokens=$null; $parseErrors=$null; [System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$parseErrors) | Out-Null; "
        "foreach($e in $parseErrors){ $errors += ($file + ':' + $e.Extent.StartLineNumber + ': ' + $e.Message) } }; "
        "if($errors.Count){ $errors; exit 1 }"
    )
    result = powershell(command)
    assert result.returncode == 0, result.stderr or result.stdout


def test_prod_verification_contract_binds_promotion_manifest_process_and_run_record(tmp_path: Path) -> None:
    candidate_root = tmp_path / "Candidate-beta.14"
    app = candidate_root / "app"
    app.mkdir(parents=True)
    data = tmp_path / "prod-data"
    cache = tmp_path / "prod-cache"
    data.mkdir()
    cache.mkdir()
    run_dir = tmp_path / "run-center"
    run_dir.mkdir()
    commit = "0123456789ab" + "c" * 28
    source_sha = "a" * 64
    manifest = {
        "version": "0.5.0-beta.14",
        "build_id": "0.5.0-beta.14+0123456789ab",
        "commit": commit,
        "source_sha256": source_sha,
    }
    manifest_path = app / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    promotion_path = candidate_root / "PROMOTION.json"
    promotion = {
        "status": "promoted_pending_prod_verification",
        "version": manifest["version"],
        "build_id": manifest["build_id"],
        "commit": commit,
        "source_sha256": source_sha,
        "target_app": str(app),
    }
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    record_path = run_dir / "PROD.json"
    verification_path = candidate_root / "PROD_VERIFICATION-test.json"
    process = {"role": "app-runtime", "pid": 2345, "executable": str(tmp_path / "runtime/pythonw.exe"), "identity_token": "desktop_bootstrap.py"}
    record = {
        "profile": "PROD",
        "build_id": manifest["build_id"],
        "commit": commit,
        "data": str(data),
        "cache": str(cache),
        "prod_verification_path": str(verification_path),
        "prod_verification_sha256": "0" * 64,
        "processes": [process],
    }
    verification = {
        "schema": 1,
        "status": "verified_live_prod",
        "profile": "prod",
        "instance_id": "PROD",
        "version": manifest["version"],
        "build_id": manifest["build_id"],
        "commit": commit,
        "source_sha256": source_sha,
        "manifest_sha256": sha256(manifest_path),
        "app_manifest_sha256": sha256(manifest_path),
        "app": str(app),
        "promotion_path": str(promotion_path),
        "promotion_sha256": sha256(promotion_path),
        "run_record": str(record_path),
        "data_path": str(data),
        "cache_path": str(cache),
        "process": process,
    }
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    record["prod_verification_sha256"] = sha256(verification_path)
    record_path.write_text(json.dumps(record), encoding="utf-8")

    def verify() -> subprocess.CompletedProcess[str]:
        module = (ROOT / "scripts/dev-release-gates.psm1").as_posix()
        return powershell(
            f"Import-Module '{module}' -Force; Test-DgProdVerification '{verification_path}' '{promotion_path}' '{record_path}' '{app}' '{data}' '{cache}' | Out-Null"
        )

    assert verify().returncode == 0
    verification["profile"] = "test"
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    record["prod_verification_sha256"] = sha256(verification_path)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    rejected = verify()
    assert rejected.returncode != 0
    assert "do not bind the same production identity" in rejected.stderr


def test_prod_backup_gate_requires_hashes_and_isolated_restore(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    data = tmp_path / "prod-data"
    cache = tmp_path / "prod-cache"
    data.mkdir()
    cache.mkdir()
    backup_dir = root / "backup-run-1"
    restore_root = backup_dir / "restore-validation"
    restore_root.mkdir(parents=True)
    db_backup = backup_dir / "workbench.db"
    reasonix_backup = backup_dir / "reasonix-sessions.zip"
    restored_db = restore_root / "workbench.db"
    for path, content in ((db_backup, b"db snapshot"), (reasonix_backup, b"sessions"), (restored_db, b"restored db")):
        path.write_bytes(content)
    report_path = backup_dir / "backup-readiness.json"

    def report(ids: list[str]) -> dict[str, object]:
        return {
            "schema": 1,
            "status": "passed",
            "profile": "prod",
            "run_id": "backup-run-1",
            "source_data_path": str(data),
            "source_cache_path": str(cache),
            "artifacts": [
                {"kind": "database", "path": str(db_backup), "sha256": sha256(db_backup)},
                {"kind": "reasonix_sessions", "path": str(reasonix_backup), "sha256": sha256(reasonix_backup)},
            ],
            "restore_validation": {
                "status": "passed",
                "restore_root": str(restore_root),
                "restored_database_path": str(restored_db),
                "restored_database_sha256": sha256(restored_db),
                "original_reasonix_session_ids": ["session-a", "session-b"],
                "restored_reasonix_session_ids": ids,
                "reasonix_restore_status": "passed",
            },
        }

    def validate() -> subprocess.CompletedProcess[str]:
        report_path.write_text(json.dumps(report(["session-a", "session-b"])), encoding="utf-8")
        return invoke_gate(
            tmp_path,
            "Test-DgProdBackupReadiness",
            f"-ReportPath '{report_path}' -BackupsRoot '{root}' -ExpectedData '{data}' -ExpectedCache '{cache}'",
        )

    accepted = validate()
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["session_ids"] == ["session-a", "session-b"]

    report_path.write_text(json.dumps(report(["session-a"])), encoding="utf-8")
    rejected_sessions = invoke_gate(
        tmp_path,
        "Test-DgProdBackupReadiness",
        f"-ReportPath '{report_path}' -BackupsRoot '{root}' -ExpectedData '{data}' -ExpectedCache '{cache}'",
    )
    assert rejected_sessions.returncode != 0
    assert "did not preserve every" in rejected_sessions.stderr

    bad = report(["session-a", "session-b"])
    bad["restore_validation"]["restore_root"] = str(data)
    report_path.write_text(json.dumps(bad), encoding="utf-8")
    rejected_prod_path = invoke_gate(
        tmp_path,
        "Test-DgProdBackupReadiness",
        f"-ReportPath '{report_path}' -BackupsRoot '{root}' -ExpectedData '{data}' -ExpectedCache '{cache}'",
    )
    assert rejected_prod_path.returncode != 0
    assert "outside its approved root" in rejected_prod_path.stderr


def test_packaged_dev_candidate_stop_never_uses_source_api_kill_fallback() -> None:
    source = (ROOT / "scripts/run-center.ps1").read_text(encoding="utf-8")
    assert "if ($Profile -ne 'DEV' -or $record.dev_candidate_report)" in source
    candidate_guard = "if ($record.dev_candidate_report) {\n            throw 'The packaged DEV candidate did not stop gracefully."
    assert candidate_guard in source
    assert "if ($devCandidateBundle) { $env:GEWU_DEV_EXTERNAL_EFFECTS = 'offline' }" in source
    assert "[int]$child.ParentProcessId -ne [int]$recordedApp.pid" in source
    assert "$childCreated -gt ([long]$recordedApp.creation_utc_ticks + [TimeSpan]::FromMinutes(5).Ticks)" in source
    assert "Test-FrozenListenerIdentity ([int]$serviceOwner) $bundle.manifest" in source
