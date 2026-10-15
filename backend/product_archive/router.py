from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from . import linked, service

router = APIRouter(prefix="/api/product-archive", tags=["Product Archive"])


def _run(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc


@router.get("/datasets")
def datasets():
    return service.list_datasets()


@router.get("/archives")
def archives():
    return _run(service.list_archives)


@router.post("/archives")
def create_archive(body: dict):
    return _run(service.create, str(body.get("dataset_id") or ""), body.get("marketplace"),
                body.get("category", ""), body.get("data_month", ""))


@router.post("/datasets/{dataset_id}/ensure-archive")
def ensure_dataset_archive(dataset_id: str, body: dict):
    return _run(service.ensure_dataset_archive, dataset_id, (body or {}).get("marketplace", ""))


@router.get("/archives/{archive_id}")
def archive(archive_id: str):
    return _run(service.get, archive_id)


@router.get("/archives/{archive_id}/review-jobs")
def review_jobs(archive_id: str):
    return _run(linked.list_review_jobs, archive_id)


@router.get("/archives/{archive_id}/reviews")
def review_workbook(archive_id: str, job_id: str, sheet: str = "", offset: int = 0,
                    limit: int = 50, query: str = ""):
    return _run(linked.review_workbook_page, archive_id, job_id, sheet_name=sheet,
                offset=offset, limit=limit, query=query)


@router.get("/archives/{archive_id}/voc-runs")
def voc_runs(archive_id: str):
    return _run(linked.list_voc_runs, archive_id)


@router.post("/archives/{archive_id}/filter")
def archive_filter(archive_id: str, body: dict):
    return _run(service.get, archive_id, (body or {}).get("filters") or {})


@router.patch("/archives/{archive_id}")
def update_archive(archive_id: str, body: dict):
    return _run(service.update_overview, archive_id, body or {})


@router.post("/archives/{archive_id}/dataset")
def bind_dataset(archive_id: str, body: dict):
    return _run(service.bind_dataset, archive_id, str((body or {}).get("dataset_id") or ""))


@router.put("/archives/{archive_id}/mapping")
def confirm_mapping(archive_id: str, body: dict):
    return _run(service.confirm_mapping, archive_id, (body or {}).get("mapping"))


@router.get("/archives/{archive_id}/samples")
def samples(archive_id: str, query: str = "", offset: int = 0, limit: int = 50):
    return _run(service.preview, archive_id, query, offset, limit)


@router.post("/import")
async def import_sample(file: UploadFile = File(...), marketplace: str = Form("US"),
                        category: str = Form(""), data_month: str = Form(""),
                        workspace_id: str = Form("")):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="请上传 .xlsx 文件")
    data = await file.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 100 MB")
    return _run(service.import_workbook, data, file.filename, marketplace, category, data_month, workspace_id)


@router.post("/archives/{archive_id}/import")
async def replace_sample(archive_id: str, file: UploadFile = File(...), workspace_id: str = Form("")):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="请上传 .xlsx 文件")
    data = await file.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 100 MB")
    return _run(service.import_into_archive, archive_id, data, file.filename, workspace_id)
