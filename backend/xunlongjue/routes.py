"""HTTP contract for the Xunlongjue integration."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import service, settings

router = APIRouter(prefix='/api/xunlongjue')


def _translate(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get('/settings')
def get_settings():
    return settings.status()


@router.put('/settings')
def put_settings(body: dict):
    return _translate(lambda: settings.save((body or {}).get('endpoint'),
                                            (body or {}).get('api_key')))


@router.get('/runs')
def get_runs():
    return {'runs': service.list_runs()}


@router.post('/runs')
def post_run(body: dict):
    body = body or {}
    return _translate(lambda: service.create_run(body.get('site'), body.get('node_id'),
                                                  body.get('max_depth'), body.get('max_nodes')))


@router.get('/runs/{run_id}')
def run_detail(run_id: str):
    return _translate(lambda: service.get_run(run_id))


@router.get('/runs/{run_id}/report')
def run_report(run_id: str):
    path = _translate(lambda: service.report_file(run_id))
    return FileResponse(path, filename=path.name, media_type='text/html; charset=utf-8')


@router.post('/runs/{run_id}/pause')
def pause_run(run_id: str):
    return _translate(lambda: service.pause(run_id))


@router.post('/runs/{run_id}/resume')
def resume_run(run_id: str):
    return _translate(lambda: service.resume(run_id))
