import json
from pathlib import Path

from backend.product_archive import service as archive_service
from backend.product_archive import sync


class _FakeDatabase:
    def one(self, _query, _params):
        return None


class _FakeStorage:
    def __init__(self, root: Path):
        self.root = root

    def revision_dir(self, project_id: str, run_id: str, revision: int) -> Path:
        return self.root / project_id / run_id / str(revision)


class _FakeVocService:
    def __init__(self, root: Path):
        self.db = _FakeDatabase()
        self.storage = _FakeStorage(root)

    def get_run(self, run_id: str):
        return {
            "id": run_id,
            "status": "completed",
            "project_id": "voc-project",
            "current_revision": 1,
            "sources": {"reviews_sha256": "a" * 64},
        }

    def project(self, _project_id: str):
        return {"workspace_id": "workspace-1"}


def test_legacy_voc_links_only_to_unique_archive_in_project_workspace(monkeypatch, tmp_path):
    archive = {"id": "archive-1", "workspace_id": "workspace-1", "dataset_id": "dataset-1",
               "overview": {}, "overview_sources": {}}
    saved = []
    monkeypatch.setattr(archive_service, "list_archives", lambda: [
        {"id": "archive-1", "dataset": {"id": "dataset-1", "workspace_id": "workspace-1"}},
        {"id": "archive-2", "dataset": {"id": "dataset-2", "workspace_id": "workspace-2"}},
    ])
    monkeypatch.setattr(archive_service, "_read", lambda archive_id: dict(archive))
    monkeypatch.setattr(archive_service, "_save", lambda record: saved.append(record))

    analysis_path = tmp_path / "voc-project" / "run-1" / "1" / "cache" / "analysis.json"
    analysis_path.parent.mkdir(parents=True)
    analysis_path.write_text(json.dumps({
        "counts": {"reviews": 17},
        "validation": {"all_green": True},
        "segments": [{"segment_id": "formal-1", "status": "formal",
                      "metrics": {"review_count": 10},
                      "jtbd_statement": "Helps with play", "persona_summary": "Dog owners"}],
    }), encoding="utf-8")

    result = sync.sync_voc_run(_FakeVocService(tmp_path), "run-1")

    assert result == {"status": "synced", "revision": 1}
    assert len(saved) == 1
    insight = saved[0]["integrations"]["amazon_voc"]
    assert insight["source_linkage"] == "unique_project_workspace"
    assert insight["review_count"] == 17
    assert insight["requested_asin_count"] is None
    assert saved[0]["overview"].get("product_function", "") == ""
    assert saved[0]["overview_suggestions"]["product_function"][0]["text"] == "Helps with play"


def test_legacy_voc_does_not_guess_when_project_workspace_has_multiple_archives(monkeypatch, tmp_path):
    monkeypatch.setattr(archive_service, "list_archives", lambda: [
        {"id": "archive-1", "dataset": {"id": "dataset-1", "workspace_id": "workspace-1"}},
        {"id": "archive-2", "dataset": {"id": "dataset-2", "workspace_id": "workspace-1"}},
    ])

    result = sync.sync_voc_run(_FakeVocService(tmp_path), "run-1")

    assert result == {"status": "ambiguous_workspace_archive"}
