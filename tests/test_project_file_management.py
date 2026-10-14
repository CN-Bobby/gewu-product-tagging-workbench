import hashlib

import pytest

from backend import project_files
from backend.project_library import FOLDER_ORDER, list_library
from backend.templates import create_project


def test_upload_records_hash_identity_and_library_categories():
    project = create_project("Managed library")
    content = b"<html><body>VOC</body></html>"
    item = project_files.upload(project["id"], "report.html", content, kind="voc",
                                source_type="manual_upload", source_id="voc:42")

    assert item["sha256"] == hashlib.sha256(content).hexdigest()
    assert item["source_type"] == "manual_upload"
    assert item["source_id"] == "voc:42"
    assert item["version_group"]
    assert project_files.artifact_path(project["id"], item["id"]).read_bytes() == content

    result = list_library(project["id"])
    assert tuple(folder["kind"] for folder in result["folders"]) == FOLDER_ORDER
    voc = next(folder for folder in result["folders"] if folder["kind"] == "voc")
    assert voc["items"][0]["id"] == item["id"]
    assert voc["items"][0]["previewable"] is True
    assert next(folder for folder in result["folders"] if folder["kind"] == "keywords")["count"] == 0


def test_upload_rejects_collision_without_overwrite_or_unsupported_format():
    project = create_project("Upload collision")
    first = project_files.upload(project["id"], "notes.html", b"first", kind="other")
    path = project_files.artifact_path(project["id"], first["id"])

    with pytest.raises(ValueError, match="已存在"):
        project_files.upload(project["id"], "notes.html", b"second", kind="other")
    assert path.read_bytes() == b"first"
    with pytest.raises(ValueError, match="仅支持"):
        project_files.upload(project["id"], "notes.json", b"{}", kind="other")


def test_recycle_restore_uses_per_project_trash_and_preserves_record():
    project = create_project("Trash restore")
    item = project_files.upload(project["id"], "reviews.xlsx", b"workbook bytes", kind="reviews")
    original = project_files.artifact_path(project["id"], item["id"])

    trashed = project_files.recycle(project["id"], item["id"])
    assert trashed["deleted_at"]
    assert trashed["downloadable"] is False
    assert not original.exists()
    trash_path = project_files.project_dir(project["id"]) / trashed["relative_path"]
    assert "_system" in trash_path.parts and "trash" in trash_path.parts
    assert trash_path.read_bytes() == b"workbook bytes"
    assert project_files.list_trash(project["id"])[0]["id"] == item["id"]
    assert all(row["id"] != item["id"] for row in project_files.list_artifacts(project["id"]))

    restored = project_files.restore(project["id"], item["id"])
    assert restored["deleted_at"] is None
    assert project_files.artifact_path(project["id"], item["id"]).read_bytes() == b"workbook bytes"
    assert project_files.list_trash(project["id"]) == []


def test_move_and_rename_preserve_source_identity_and_reject_collision(tmp_path):
    first = create_project("Move source")
    second = create_project("Move destination")
    item = project_files.upload(first["id"], "raw.xlsx", b"file data", kind="source",
                                source_type="dataset", source_id="dataset-id")
    moved = project_files.update(first["id"], item["id"], name="renamed.xlsx",
                                 kind="keywords", target_workspace_id=second["id"])

    assert moved["workspace_id"] == second["id"]
    assert moved["name"] == "renamed.xlsx"
    assert moved["kind"] == "keywords"
    assert moved["source_type"] == "dataset" and moved["source_id"] == "dataset-id"
    assert moved["sha256"] == item["sha256"]
    assert project_files.artifact_path(second["id"], item["id"]).read_bytes() == b"file data"
    with pytest.raises(ValueError):
        project_files.artifact_path(first["id"], item["id"])

    collision = project_files.upload(second["id"], "occupied.html", b"existing", kind="other")
    with pytest.raises(ValueError):
        project_files.update(second["id"], item["id"], name="occupied.html", kind="other")
    assert project_files.artifact_path(second["id"], collision["id"]).read_bytes() == b"existing"


def test_filename_does_not_define_version_group():
    project = create_project("Source identity")
    a = project_files.upload(project["id"], "same.html", b"a", kind="other",
                             source_type="manual_upload", source_id="source-a")
    b = project_files.upload(project["id"], "same-copy.html", b"b", kind="other",
                             source_type="manual_upload", source_id="source-b")
    version = project_files.upload(project["id"], "renamed.html", b"next", kind="other",
                                   source_type="manual_upload", source_id="source-a")
    assert a["version_group"] != b["version_group"]
    assert a["version_group"] == version["version_group"]


def test_database_failure_cleans_up_new_file(monkeypatch):
    project = create_project("Upload rollback")

    def fail_begin(*_args, **_kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(project_files.Session, "begin", fail_begin)
    with pytest.raises(RuntimeError, match="database write failed"):
        project_files.upload(project["id"], "failure.html", b"content", kind="other")
    target = project_files.project_dir(project["id"]) / project_files.FOLDERS["other"] / "failure.html"
    assert not target.exists()


def test_external_reference_cannot_be_moved_or_recycled(tmp_path):
    project = create_project("External file")
    external = tmp_path / "external.xlsx"
    external.write_bytes(b"external")
    item = project_files.register_file(project["id"], "source", external,
                                       source_type="dataset", source_id="external-id",
                                       copy_file=False)
    with pytest.raises(ValueError, match="外部引用"):
        project_files.update(project["id"], item["id"], name="copy.xlsx")
    with pytest.raises(ValueError, match="外部引用"):
        project_files.recycle(project["id"], item["id"])
    assert external.read_bytes() == b"external"
