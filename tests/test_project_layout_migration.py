import hashlib
import json
import sqlite3
from pathlib import Path

from scripts import migrate_project_layout as migration


WORKSPACE_ID = "a" * 32


def _database(data_dir, artifacts):
    database = data_dir / "workbench.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE workspace (id TEXT PRIMARY KEY)")
        connection.execute("""CREATE TABLE project_artifact (
            id TEXT PRIMARY KEY, workspace_id TEXT, kind TEXT, relative_path TEXT,
            sha256 TEXT, size INTEGER, metadata_json TEXT)""")
        connection.execute("INSERT INTO workspace (id) VALUES (?)", (WORKSPACE_ID,))
        connection.executemany(
            "INSERT INTO project_artifact VALUES (?, ?, ?, ?, ?, ?, ?)", artifacts)
    return database


def _artifact(artifact_id, kind, relative_path, content, metadata=None):
    return (artifact_id, WORKSPACE_ID, kind, relative_path,
            hashlib.sha256(content).hexdigest(), len(content),
            json.dumps(metadata or {}))


def test_migration_dry_run_is_read_only_and_maps_visible_and_system_files(tmp_path):
    data_dir = tmp_path / "Data"
    root = data_dir / "projects" / WORKSPACE_ID
    source = root / "source" / "raw" / "source.xlsx"
    module_file = root / "product-archive" / "archive.html"
    unregistered = root / "other" / "manual.txt"
    for path, content in ((source, b"source"), (module_file, b"archive"),
                          (unregistered, b"manual")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _database(data_dir, [
        _artifact("b" * 32, "source", "source/raw/source.xlsx", b"source"),
        _artifact("c" * 32, "product_archive", "product-archive/archive.html", b"archive"),
    ])

    plan = migration.build_plan(data_dir)

    assert plan["conflicts"] == []
    assert plan["file_move_count"] == 2
    assert (root / "source" / "raw" / "source.xlsx").is_file()
    assert not (root / "source" / "source.xlsx").exists()
    targets = {move.destination.relative_to(root).as_posix() for move in plan["moves"]}
    assert targets == {"source/source.xlsx", "_system/product-archive/archive.html"}
    assert unregistered.is_file()


def test_migration_apply_preserves_hashes_records_trash_and_external_refs(tmp_path):
    data_dir = tmp_path / "Data"
    root = data_dir / "projects" / WORKSPACE_ID
    files = {
        "source/raw/source.xlsx": b"source",
        "product-archive/archive.html": b"archive",
        ".trash/" + "d" * 32 + "-reviews.xlsx": b"deleted",
        "other/manual.txt": b"manual",
    }
    for relative, content in files.items():
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _database(data_dir, [
        _artifact("b" * 32, "source", "source/raw/source.xlsx", b"source"),
        _artifact("c" * 32, "product_archive", "product-archive/archive.html", b"archive"),
        _artifact("d" * 32, "reviews", ".trash/" + "d" * 32 + "-reviews.xlsx", b"deleted",
                  {"deleted_at": "2026-01-01T00:00:00Z", "restore_path": "reviews/reviews.xlsx"}),
        ("e" * 32, WORKSPACE_ID, "source", "external:C:/legacy.xlsx", "0" * 64, 10, "{}"),
    ])

    plan = migration.build_plan(data_dir)
    report = migration.apply_plan(plan)

    assert report.is_file()
    assert (root / "source" / "source.xlsx").read_bytes() == b"source"
    assert (root / "_system" / "product-archive" / "archive.html").read_bytes() == b"archive"
    assert (root / "_system" / "trash" / ("d" * 32 + "-reviews.xlsx")).read_bytes() == b"deleted"
    assert (root / "other" / "manual.txt").read_bytes() == b"manual"
    assert not (root / "source" / "raw").exists()
    assert not (root / "product-archive").exists()
    for relative in ("source", "data-x", "reviews", "voc", "keywords", "other"):
        assert (root / relative).is_dir()
    with sqlite3.connect(data_dir / "workbench.db") as connection:
        rows = {row[0]: (row[1], json.loads(row[2])) for row in connection.execute(
            "SELECT id, relative_path, metadata_json FROM project_artifact")}
    assert rows["b" * 32][0] == "source/source.xlsx"
    assert rows["c" * 32][0] == "_system/product-archive/archive.html"
    assert rows["d" * 32][0] == "_system/trash/" + "d" * 32 + "-reviews.xlsx"
    assert rows["d" * 32][1]["restore_path"] == "reviews/reviews.xlsx"
    assert rows["e" * 32][0] == "external:C:/legacy.xlsx"
    rerun = migration.build_plan(data_dir)
    assert rerun["conflicts"] == []
    assert rerun["file_move_count"] == 0
    assert rerun["record_update_count"] == 0


def test_conflicting_destination_blocks_migration_and_keeps_both_files(tmp_path):
    data_dir = tmp_path / "Data"
    root = data_dir / "projects" / WORKSPACE_ID
    source = root / "source" / "raw" / "source.xlsx"
    destination = root / "source" / "source.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"registered source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"different existing file")
    _database(data_dir, [
        _artifact("b" * 32, "source", "source/raw/source.xlsx", b"registered source"),
    ])

    plan = migration.build_plan(data_dir)

    assert any("SHA-256 不同" in conflict for conflict in plan["conflicts"])
    assert source.read_bytes() == b"registered source"
    assert destination.read_bytes() == b"different existing file"


def test_partial_migration_skips_conflicted_project_and_migrates_safe_project(tmp_path):
    data_dir = tmp_path / "Data"
    projects = data_dir / "projects"
    blocked_root = projects / WORKSPACE_ID
    safe_id = "f" * 32
    safe_root = projects / safe_id
    (blocked_root / "source").mkdir(parents=True)
    (blocked_root / "source" / "source.xlsx").write_bytes(b"conflicting destination")
    safe_source = safe_root / "source" / "raw" / "safe.xlsx"
    safe_source.parent.mkdir(parents=True)
    safe_source.write_bytes(b"safe")
    database = data_dir / "workbench.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE workspace (id TEXT PRIMARY KEY)")
        connection.execute("""CREATE TABLE project_artifact (
            id TEXT PRIMARY KEY, workspace_id TEXT, kind TEXT, relative_path TEXT,
            sha256 TEXT, size INTEGER, metadata_json TEXT)""")
        connection.executemany("INSERT INTO workspace (id) VALUES (?)",
                               ((WORKSPACE_ID,), (safe_id,)))
        connection.executemany(
            "INSERT INTO project_artifact VALUES (?, ?, ?, ?, ?, ?, ?)", [
                _artifact("b" * 32, "source", "source/raw/missing.xlsx", b"missing"),
                ("c" * 32, safe_id, "source", "source/raw/safe.xlsx",
                 hashlib.sha256(b"safe").hexdigest(), 4, "{}"),
            ])

    plan = migration.build_plan(data_dir)
    assert plan["blocked_project_ids"] == [WORKSPACE_ID]
    report = migration.apply_plan(plan)

    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "partial"
    assert safe_root.joinpath("source", "safe.xlsx").read_bytes() == b"safe"
    assert not safe_source.exists()
    assert blocked_root.joinpath("source", "source.xlsx").read_bytes() == b"conflicting destination"
    with sqlite3.connect(database) as connection:
        paths = dict(connection.execute("SELECT id, relative_path FROM project_artifact"))
    assert paths["c" * 32] == "source/safe.xlsx"
    assert paths["b" * 32] == "source/raw/missing.xlsx"
