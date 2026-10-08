import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_project_library.py"


def _storage(tmp_path: Path) -> tuple[Path, Path, str]:
    storage = tmp_path / "storage"
    storage.mkdir()
    source = tmp_path / "legacy" / "reviews.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"verified review workbook")
    workspace_id = "a" * 32
    with sqlite3.connect(storage / "workbench.db") as connection:
        connection.executescript("""
            CREATE TABLE workspace (
              id VARCHAR(32) PRIMARY KEY, created_at VARCHAR, name VARCHAR NOT NULL
            );
            CREATE TABLE project_artifact (
              id VARCHAR(32) PRIMARY KEY, created_at VARCHAR, workspace_id VARCHAR(32) NOT NULL,
              kind VARCHAR NOT NULL, source_type VARCHAR NOT NULL, source_id VARCHAR NOT NULL,
              name VARCHAR NOT NULL, relative_path VARCHAR NOT NULL, sha256 VARCHAR NOT NULL,
              size INTEGER NOT NULL, metadata_json JSON NOT NULL
            );
        """)
        connection.execute("INSERT INTO workspace VALUES (?, ?, ?)", (workspace_id, "2026-10-01T00:00:00Z", "Legacy project"))
        import hashlib
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        connection.execute("""INSERT INTO project_artifact
            (id, created_at, workspace_id, kind, source_type, source_id, name, relative_path,
             sha256, size, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("b" * 32, "2026-10-01T00:00:00Z", workspace_id, "reviews",
             "sorftime_mcp_review_export", "legacy-export-1", "reviews.xlsx",
             "external:" + str(source.resolve()), digest, source.stat().st_size, "{}"))
        # A raw API response is deliberately ineligible for the project library.
        connection.execute("""INSERT INTO project_artifact
            (id, created_at, workspace_id, kind, source_type, source_id, name, relative_path,
             sha256, size, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("c" * 32, "2026-10-01T00:00:01Z", workspace_id, "reviews",
             "amazon_reviews", "job-1:raw.json", "Amazon-Reviews-raw.json",
             "external:" + str(source.resolve()), digest, source.stat().st_size, "{}"))
    return storage, source, workspace_id


def _run(storage: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TAG_STORAGE", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--storage", str(storage), *args],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )


def test_dry_run_is_read_only_and_only_lists_deliverables(tmp_path):
    storage, source, workspace_id = _storage(tmp_path)
    result = _run(storage)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry_run"
    assert report["counts"] == {"ready": 1}
    assert report["items"][0]["workspace_id"] == workspace_id
    assert source.exists()
    assert not (storage / "projects").exists()
    with sqlite3.connect(storage / "workbench.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_artifact").fetchone()[0] == 2


def test_apply_copies_without_moving_and_is_idempotent(tmp_path):
    storage, source, workspace_id = _storage(tmp_path)
    original = source.read_bytes()
    report_path = tmp_path / "reports" / "backfill.json"
    applied = _run(storage, "--apply", "--report", str(report_path))
    assert applied.returncode == 0, applied.stderr
    report = json.loads(applied.stdout)
    assert report["counts"] == {"backfilled": 1}
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["mode"] == "apply"
    assert source.read_bytes() == original
    target = next((storage / "projects" / workspace_id / "reviews").glob("reviews-*.xlsx"))
    assert target.read_bytes() == original
    with sqlite3.connect(storage / "workbench.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_artifact").fetchone()[0] == 3

    repeated = _run(storage, "--apply", "--report", str(tmp_path / "reports" / "repeat.json"))
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["counts"] == {"already_backfilled": 1}
    with sqlite3.connect(storage / "workbench.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_artifact").fetchone()[0] == 3


def test_hash_mismatch_is_reported_without_copying(tmp_path):
    storage, source, _ = _storage(tmp_path)
    source.write_bytes(b"changed since legacy registration")
    result = _run(storage, "--apply", "--report", str(tmp_path / "reports" / "mismatch.json"))
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["counts"] == {"source_hash_mismatch": 1}
    assert not (storage / "projects").exists()
