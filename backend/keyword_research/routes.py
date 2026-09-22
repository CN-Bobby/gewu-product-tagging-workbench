"""关键词调研的 HTTP 面：表单进，快照与事件出。

这一层只认识运行编号、预算与产物，不认识引擎怎么跑（那是 runner 的事）。输入口径一律
经 pipeline.prepare_task_input 组装，不在这里另拼一份 options——那份口径的真相源只有一个。
所有校验失败统一成 400 + {'detail': {'errors': [...]}}：前端一次展示全部问题。
"""
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..db import now
from . import budget, credentials, registry, runner, stream
from .input_validator import MARKETPLACES, ValidationError as InputError
from .pipeline import prepare_task_input

router = APIRouter(prefix='/api/keyword-research')

XLSX_MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _refuse(*messages):
    raise HTTPException(status_code=400, detail={'errors': [str(m) for m in messages]})


def _task_input(body):
    """校验并组装 input.json 结构。矩阵开关进 options，因此它随 input.json 一起冻结。"""
    if not isinstance(body, dict):
        _refuse('请求体必须是对象')
    try:
        return prepare_task_input(body.get('groups') or [], body.get('data_month'),
                                  body.get('matrix_month'),
                                  options={'matrix_enabled': bool(body.get('matrix_enabled', True))})
    except InputError as exc:
        _refuse(*exc.errors)


def _plan_hash(task_input):
    canonical = json.dumps(task_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _budget_of(task_input):
    return budget.plan_budget(task_input['groups'], task_input['options']).as_dict()


def _snapshot(run_id):
    state = registry.read(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    return state


@router.get('/meta')
def meta():
    """站点与取数口径来自后端常量：前端不许自己硬编码一份。"""
    return {'marketplaces': sorted(MARKETPLACES), 'node_top_n_options': [50, 100],
            'matrix_default': True, 'credential': credentials.status()}


@router.get('/credentials')
def credential_status():
    return credentials.status()


@router.post('/credentials')
def credential_save(body: dict):
    return credentials.save_secret((body or {}).get('secret_key'))


@router.delete('/credentials')
def credential_clear():
    return credentials.clear_secret()


@router.post('/plan')
def plan(body: dict):
    task_input = _task_input(body)
    return {'plan_hash': _plan_hash(task_input), 'budget': _budget_of(task_input)}


@router.post('/runs')
def create_run(body: dict):
    task_input = _task_input(body)
    running = [item for item in registry.list_runs(limit=200) if item['status'] in registry.ACTIVE]
    if running:
        _refuse('已有一次调研在运行（%s），请等它结束或先取消' % running[0]['run_id'])
    if str((body or {}).get('plan_hash') or '') != _plan_hash(task_input):
        _refuse('预算已变化，请重新核算后再发起')
    run_id = registry.create(task_input, {'confirmed': True, 'created_at': now(),
                                          'budget': _budget_of(task_input)})
    runner.launch(run_id)
    return {'run_id': run_id, 'status': registry.QUEUED}


@router.get('/runs')
def list_runs():
    return {'runs': registry.list_runs()}


@router.get('/runs/{run_id}')
def run_detail(run_id: str):
    return _snapshot(run_id)


@router.get('/runs/{run_id}/events')
async def run_events(run_id: str, request: Request, after_sequence: str | None = None):
    from fastapi.responses import StreamingResponse
    if registry.read(run_id) is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    cursor = stream.resolve_cursor(after_sequence, request.headers.get('last-event-id'))
    return StreamingResponse(
        stream.event_stream(run_id, cursor, disconnected=request.is_disconnected),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/runs/{run_id}/cancel')
def cancel_run(run_id: str):
    _snapshot(run_id)
    return {'status': runner.cancel(run_id)}


@router.get('/runs/{run_id}/artifacts/{name}')
def artifact(run_id: str, name: str):
    from fastapi.responses import FileResponse
    _snapshot(run_id)
    path = registry.artifact_path(run_id, name)
    return FileResponse(path, filename=Path(name).name, media_type=XLSX_MEDIA)
