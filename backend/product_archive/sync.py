"""Bridge completed keyword and VOC artifacts into their source archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_keyword_rows(run_dir: Path) -> list[dict[str, Any]]:
    for name in ("keyword_labeled.json", "keyword_master.json"):
        path = run_dir / "cache" / name
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("keywords") if isinstance(document, dict) else None
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def sync_keyword_archive(archive_id: str) -> dict[str, Any]:
    """Rebuild keyword insights from every completed run bound to this archive."""
    from . import service as archive_service
    from .insights import merge_keyword_insights
    from ..keyword_research import registry

    archive = archive_service._read(archive_id)
    runs: list[dict[str, Any]] = []
    for state in registry.all_runs():
        run_id = str(state.get("run_id") or "")
        if (state.get("status") != registry.DONE
                or state.get("source_archive_id") != archive_id
                or state.get("source_dataset_id") != archive.get("dataset_id")
                or not run_id):
            continue
        keyword_rows = _load_keyword_rows(registry.run_dir(run_id))
        if not keyword_rows:
            continue
        try:
            plan = registry.plan_of(run_id)
        except (OSError, ValueError):
            plan = {}
        runs.append({
            "run_id": run_id,
            "data_month": str(state.get("data_month") or plan.get("data_month") or ""),
            "updated_at": str(state.get("updated_at") or state.get("created_at") or ""),
            "keywords": keyword_rows,
        })
    if not runs:
        return {"status": "no_completed_keyword_runs", "run_count": 0}
    runs.sort(key=lambda item: item["run_id"])
    merged = merge_keyword_insights(archive, runs)
    archive_service._save(merged)
    result = ((merged.get("integrations") or {}).get("keyword_research") or {})
    return {"status": "synced", "run_count": len(runs),
            "representative_term_count": len(result.get("representative_terms") or [])}


def sync_voc_run(voc_service, run_id: str) -> dict[str, Any]:
    """Sync a completed VOC run to its source archive without new model calls."""
    from . import service as archive_service
    from .insights import merge_voc_insights

    run = voc_service.get_run(run_id)
    if not run or run.get("status") != "completed":
        return {"status": "not_completed"}
    project = voc_service.project(run.get("project_id") or "") or {}
    workspace_id = str(project.get("workspace_id") or "")
    source = run.get("sources") if isinstance(run.get("sources"), dict) else {}
    source_context = source.get("review_source_context")
    source_context = dict(source_context) if isinstance(source_context, dict) else {}
    source_linkage = "reviews_context"

    # Recover older, manually initiated Reviews -> VOC handoffs from their local
    # idempotency ledger when the run snapshot predates source-context persistence.
    if not source_context.get("source_job_id"):
        try:
            handoff = voc_service.db.one(
                "SELECT source_job_id FROM workbench_reviews_handoffs WHERE run_id=?",
                (run_id,),
            )
        except (AttributeError, OSError, ValueError):
            handoff = None
        if handoff and handoff.get("source_job_id"):
            source_context["source_job_id"] = handoff["source_job_id"]
            source_linkage = "reviews_handoff_ledger"

    # Older handoffs predate archive linkage in the VOC source context. Resolve
    # the linkage from the local Reviews job record, never from a caller path.
    if not source_context.get("source_archive_id") and source_context.get("source_job_id"):
        try:
            from ..amazon_reviews import service as reviews_service
            review_source = reviews_service.get(str(source_context["source_job_id"]))
        except (KeyError, ValueError, OSError):
            review_source = None
        if isinstance(review_source, dict):
            source_context["source_archive_id"] = review_source.get("source_archive_id")
            source_context["source_dataset_id"] = review_source.get("source_dataset_id")
            if review_source.get("source_archive_id"):
                source_linkage = "reviews_job"

    archive_id = str(source_context.get("source_archive_id") or "")
    if not archive_id:
        # A legacy VOC run may have been created directly from the review
        # workbook. Associate it only when its project workspace identifies one
        # and exactly one archive; ambiguous projects stay unlinked.
        candidates = [item for item in archive_service.list_archives()
                      if str((item.get("dataset") or {}).get("workspace_id") or "") == workspace_id]
        if len(candidates) != 1:
            return {"status": "ambiguous_workspace_archive" if candidates else "no_source_archive"}
        archive_id = str(candidates[0].get("id") or "")
        source_dataset_id = str((candidates[0].get("dataset") or {}).get("id") or "")
        source_linkage = "unique_project_workspace"
        source_context["source_archive_id"] = archive_id
        source_context["source_dataset_id"] = source_dataset_id
    archive = archive_service._read(archive_id)
    source_dataset_id = str(source_context.get("source_dataset_id") or archive.get("dataset_id") or "")
    if (not workspace_id or archive.get("workspace_id") != workspace_id
            or source_dataset_id != archive.get("dataset_id")):
        return {"status": "source_scope_mismatch"}

    revision = int(run.get("current_revision") or 1)
    analysis_path = (voc_service.storage.revision_dir(run["project_id"], run_id, revision)
                     / "cache" / "analysis.json")
    if not analysis_path.is_file():
        return {"status": "analysis_missing"}
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    counts = analysis.get("counts") if isinstance(analysis.get("counts"), dict) else {}
    if source_context.get("review_count") is None and counts.get("reviews") is not None:
        source_context["review_count"] = counts.get("reviews")
    if source_linkage == "unique_project_workspace":
        source_context.setdefault(
            "analysis_scope",
            "仅分析本次 VOC 输入 Excel 中的评论；原始评论任务覆盖信息未随旧任务保留。",
        )
    payload = {
        "run_id": run_id,
        "revision": revision,
        "reviews_sha256": str(source.get("reviews_sha256") or ""),
        "workspace_id": workspace_id,
        "source_archive_id": archive_id,
        "source_dataset_id": source_dataset_id,
        "updated_at": str(run.get("updated_at") or run.get("completed_at") or ""),
    }
    merged = merge_voc_insights(archive, payload, analysis, source_context)
    merged.setdefault("integrations", {}).setdefault("amazon_voc", {})["source_linkage"] = source_linkage
    archive_service._save(merged)
    return {"status": "synced", "revision": revision}
