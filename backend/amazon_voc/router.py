from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, StrictBool
from starlette.datastructures import UploadFile

from .service import MAX_REVIEW_BYTES, service


router = APIRouter(prefix="/api/amazon-voc", tags=["Amazon VOC"])


class ProjectInput(BaseModel):
    name: str
    marketplace: str = "US"
    country: str = "US"
    language: str = "en"
    category: str = ""
    workspace_id: str | None = None


class CorrectionInput(BaseModel):
    event: dict
    note: str = ""


class ReviewsHandoffInput(BaseModel):
    confirmed_reviewed: StrictBool


def _http_error(exc: Exception):
    from ..runtime_isolation import CredentialUnavailable
    if isinstance(exc, CredentialUnavailable):
        raise exc
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc).strip("'")) from exc
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, (ValueError,)):
        raise HTTPException(400, str(exc)) from exc
    raise exc


@router.get("/settings")
def settings():
    return service.public_model_settings()


@router.get("/projects")
def projects():
    return service.list_projects()


@router.post("/projects")
def create_project(item: ProjectInput):
    try:
        return service.create_project(item.model_dump())
    except Exception as exc:
        _http_error(exc)


@router.post("/projects/{project_id}/runs")
async def create_run(project_id: str, request: Request):
    form = await request.form(max_files=3, max_fields=20, max_part_size=MAX_REVIEW_BYTES)
    uploads = [(name, item) for name, item in form.multi_items() if isinstance(item, UploadFile)]
    try:
        if len(uploads) != 1 or uploads[0][0] != "reviews":
            raise HTTPException(422, "每个 VOC 任务只能上传一个评论Excel文件")
        allowed = {"reviews", "model", "credential_id", "force_refresh"}
        unexpected = [name for name, _ in form.multi_items() if name not in allowed]
        if unexpected:
            raise HTTPException(422, "评论Excel之外只能提交模型选项，不能上传其他分析数据源")
        upload = uploads[0][1]
        if Path(upload.filename or "").suffix.lower() not in {".xlsx", ".xlsm"}:
            raise HTTPException(422, "仅支持 .xlsx 或 .xlsm 评论Excel文件")
        data = await upload.read(MAX_REVIEW_BYTES + 1)
        if len(data) > MAX_REVIEW_BYTES:
            raise HTTPException(413, "评论Excel文件最大支持 50 MiB")
        force_refresh_raw = str(form.get("force_refresh") or "false").casefold()
        if force_refresh_raw not in ("true", "false", "1", "0"):
            raise HTTPException(422, "force_refresh 只能为 true 或 false")
        return service.create_run(project_id, upload.filename or "reviews.xlsx", data,
                                  form.get("model"), form.get("credential_id"),
                                  force_refresh=force_refresh_raw in ("true", "1"),
                                  analysis_mode="full_voc_v2")
    except HTTPException:
        raise
    except Exception as exc:
        _http_error(exc)
    finally:
        for _, upload in uploads:
            await upload.close()


@router.post("/from-amazon-reviews/{job_id}")
def create_run_from_amazon_reviews(job_id: str, item: ReviewsHandoffInput):
    try:
        return service.create_run_from_amazon_reviews(job_id, item.confirmed_reviewed)
    except Exception as exc:
        _http_error(exc)


@router.get("/runs")
def runs(project_id: str | None = None):
    return service.list_runs(project_id)


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    try:
        run = service.get_run(run_id)
        if not run:
            raise KeyError("任务不存在")
        return run
    except Exception as exc:
        _http_error(exc)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str):
    try:
        return service.cancel(run_id)
    except Exception as exc:
        _http_error(exc)


@router.post("/runs/{run_id}/retry")
def retry(run_id: str):
    try:
        return service.retry(run_id)
    except Exception as exc:
        _http_error(exc)


@router.get("/runs/{run_id}/artifacts/{kind}")
def artifact(run_id: str, kind: str):
    try:
        path = service.artifact_path(run_id, kind)
        media = {"html": "text/html; charset=utf-8", "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "quality": "application/json"}[kind]
        return FileResponse(path, filename=path.name, media_type=media)
    except Exception as exc:
        _http_error(exc)


@router.get("/runs/{run_id}/events")
def events(run_id: str):
    try:
        return service.cache_doc(run_id, "events.json", {"events": []})
    except Exception as exc:
        _http_error(exc)


@router.get("/runs/{run_id}/segments")
def segments(run_id: str):
    try:
        return service.cache_doc(run_id, "segment_model.json", {"segments": []})
    except Exception as exc:
        _http_error(exc)


@router.get("/runs/{run_id}/journey")
def journey(run_id: str):
    try:
        return service.cache_doc(run_id, "segment_model.json", {}).get("journey", {})
    except Exception as exc:
        _http_error(exc)


@router.patch("/runs/{run_id}/events/{event_id}")
def correct_event(run_id: str, event_id: str, item: CorrectionInput):
    try:
        return service.correct_event(run_id, event_id, item.event, item.note)
    except Exception as exc:
        _http_error(exc)


def recover():
    service.recover()
    service.start_worker()


def stop_all():
    service.stop_worker()
