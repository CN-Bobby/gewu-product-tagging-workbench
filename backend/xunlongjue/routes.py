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


def _translate_revision(call):
    try:
        return call()
    except service.RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
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
def get_runs(search: str = '', status: str = '', sort: str = 'updated_at',
             direction: str = 'desc', archived: bool | None = None,
             trashed: bool | None = None):
    return {'runs': _translate(lambda: service.list_runs(search, status, sort, direction,
                                                         archived, trashed)),
            'recovery_warnings': service.recovery_warnings()}


@router.post('/runs')
def post_run(body: dict):
    body = body or {}
    policy = body.get('policy') if isinstance(body.get('policy'), dict) else None
    return _translate(lambda: service.create_run(
        body.get('site'), body.get('node_id'), body.get('max_depth'), body.get('max_nodes'),
        category=body.get('category'), operator=body.get('operator'), policy=policy))


@router.post('/runs/{run_id}/metadata')
def run_metadata(run_id: str, body: dict):
    body = body or {}
    return _translate_revision(lambda: service.update_metadata(
        run_id, body.get('revision'), body.get('name'), body.get('note')))


@router.post('/runs/bulk')
def bulk_runs(body: dict):
    body = body or {}
    return _translate(lambda: service.bulk_runs(body.get('action'), body.get('run_ids')))


@router.get('/presets')
def get_presets():
    return {'presets': _translate(service.list_presets)}


@router.post('/presets')
def post_preset(body: dict):
    body = body or {}
    return _translate(lambda: service.create_preset(body.get('name'), body.get('policy')))


@router.put('/presets/{preset_id}')
def put_preset(preset_id: str, body: dict):
    body = body or {}
    return _translate(lambda: service.update_preset(preset_id, body.get('name'), body.get('policy')))


@router.delete('/presets/{preset_id}')
def delete_preset(preset_id: str):
    return _translate(lambda: service.delete_preset(preset_id))


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
