"""关键词调研的 HTTP 面：表单进，快照与事件出。

这一层只认识运行编号、预算与产物，不认识引擎怎么跑（那是 runner 的事）。输入口径一律
经 pipeline.prepare_task_input 组装，不在这里另拼一份 options——那份口径的真相源只有一个。
所有校验失败统一成 400 + {'detail': {'errors': [...]}}：前端一次展示全部问题。
"""
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..db import now, Session, Workspace, require
from .. import project_files
from . import budget, credentials, registry, runner, stream, project_link
from .input_validator import MARKETPLACES, ValidationError as InputError
from .pipeline import prepare_task_input
from ..product_archive import service as archive_service

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


def _plan_hash(task_input, selection=None, workspace_id=None):
    canonical = json.dumps({'input': task_input, 'selection': selection or {},
                            'workspace_id': workspace_id or None},
                           sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _workspace_id(body):
    workspace_id = str((body or {}).get('workspace_id') or '').strip() or None
    if workspace_id:
        with Session() as session:
            require(session, Workspace, workspace_id)
    return workspace_id


def _budget_of(task_input):
    return budget.plan_budget(task_input['groups'], task_input['options']).as_dict()


def _archive_budget_of(task_input):
    """Archive-dispatched live runs never retry a SellerSprite business call."""
    result = _budget_of(task_input)
    result['retry_budget'] = 0
    result['hard_cap'] = max(200, int(result.get('planned_total') or 0))
    return result


def _snapshot(run_id):
    state = registry.read(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    state = registry.public_summary(state)
    state['queue_position'] = registry.queue_position(run_id)
    state.setdefault('attempt', 0)
    state.setdefault('progress', {'stage': state.get('stage') or '', 'done': None,
                                  'total': None, 'unit': None, 'message': state.get('message') or ''})
    state.setdefault('latest_sequence', registry.latest_sequence(run_id))
    return state


def _selection(body):
    return credentials.model_selection((body or {}).get('model_ref'),
                                       (body or {}).get('credential_id'))


@router.get('/meta')
def meta():
    """站点与取数口径来自后端常量：前端不许自己硬编码一份。"""
    priority = ('US', 'UK', 'DE', 'CA', 'FR')
    marketplaces = [*priority, *sorted(MARKETPLACES.difference(priority))]
    return {'marketplaces': marketplaces, 'node_top_n_options': [50, 100],
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
    selection = _selection(body)
    workspace_id = _workspace_id(body)
    return {'plan_hash': _plan_hash(task_input, selection, workspace_id), 'selected': selection,
            'model_ref': selection['model_ref'], 'credential_id': selection['credential_id'],
            'budget': _budget_of(task_input), 'workspace_id': workspace_id}


@router.post('/runs')
def create_run(body: dict):
    task_input = _task_input(body)
    selection = _selection(body)
    workspace_id = _workspace_id(body)
    if str((body or {}).get('plan_hash') or '') != _plan_hash(task_input, selection, workspace_id):
        _refuse('预算已变化，请重新核算后再发起')
    dispatch = (body or {}).get('dispatch') or 'enqueue'  # legacy clients execute after confirmation
    if dispatch not in ('create', 'enqueue'):
        _refuse('dispatch 只能是 create 或 enqueue')
    workspace_id = str((body or {}).get('workspace_id') or '').strip() or None
    if workspace_id:
        try:
            project_files.public_project(workspace_id)
        except ValueError as exc:
            _refuse(str(exc))
    run_id = registry.create(task_input, {'confirmed': True, 'created_at': now(),
                                          'budget': _budget_of(task_input),
                                          'selection': selection,
                                          'plan_hash': _plan_hash(task_input, selection, workspace_id)},
                             display_name=(body or {}).get('display_name'), workspace_id=workspace_id)
    if workspace_id:
        run_folder = registry.run_dir(run_id)
        project_files.register_file(workspace_id, 'keywords', run_folder / 'input.json',
            source_type='keyword_run_input', source_id=run_id, name=f'{run_id}-input.json')
        project_files.register_file(workspace_id, 'keywords', run_folder / 'plan.json',
            source_type='keyword_run_plan', source_id=run_id, name=f'{run_id}-plan.json')
    if dispatch == 'enqueue':
        runner.start_queue([run_id])
    state = registry.read(run_id)
    return {'run_id': run_id, 'status': state['status'], 'attempt': state.get('attempt', 0)}


@router.post('/from-product-archive/{archive_id}/runs')
def create_archive_runs(archive_id: str, body: dict):
    """Create one matrix-free keyword run per 20 selected archive links."""
    try:
        targets = archive_service.dispatch_targets(archive_id, (body or {}).get('product_ids') or [],
                                                   child_only=bool((body or {}).get('child_only', False)))
    except ValueError as exc:
        _refuse(str(exc))
    expected_workspace = str((body or {}).get('workspace_id') or targets.get('workspace_id') or '')
    if expected_workspace != str(targets.get('workspace_id') or ''):
        _refuse('档案和选中行不属于同一项目')
    dispatch = str((body or {}).get('dispatch') or 'create')
    if dispatch not in ('create', 'enqueue'):
        _refuse('dispatch 只能是 create 或 enqueue')
    selection = _selection(body)
    chunks = [targets['asins'][offset:offset + 20] for offset in range(0, len(targets['asins']), 20)]
    if not chunks:
        _refuse('没有可用的 ASIN')
    created = []
    for index, chunk in enumerate(chunks, start=1):
        task_input = _task_input({'groups': [{'marketplace': archive_service._read(archive_id).get('marketplace', 'US'),
            'mode': 'asin', 'asins': chunk, 'node_ids': [], 'node_top_n': 50}],
            'matrix_enabled': False})
        workspace_id = targets.get('workspace_id')
        run_budget = _archive_budget_of(task_input)
        plan = {'confirmed': True, 'created_at': now(), 'budget': run_budget,
                'selection': selection, 'plan_hash': _plan_hash(task_input, selection, workspace_id),
                'source_archive_id': archive_id, 'source_dataset_id': targets['dataset_id']}
        run_id = registry.create(task_input, plan, display_name=f"档案关键词 {index}/{len(chunks)}",
                                 workspace_id=workspace_id)
        registry.write(run_id, source_archive_id=archive_id, source_dataset_id=targets['dataset_id'])
        if workspace_id:
            folder = registry.run_dir(run_id)
            project_files.register_file(workspace_id, 'keywords', folder / 'input.json',
                source_type='keyword_run_input', source_id=run_id, name=f'{run_id}-input.json')
            project_files.register_file(workspace_id, 'keywords', folder / 'plan.json',
                source_type='keyword_run_plan', source_id=run_id, name=f'{run_id}-plan.json')
        state = registry.read(run_id)
        created.append({'run_id': run_id, 'status': state['status'], 'attempt': state.get('attempt', 0),
                        'workspace_id': workspace_id, 'group_number': index,
                        'asin_count': len(chunk), 'budget': run_budget})
    if dispatch == 'enqueue':
        runner.start_queue([item['run_id'] for item in created])
        for item in created:
            item['status'] = registry.read(item['run_id']).get('status')
    return {'archive_id': archive_id, 'source_dataset_id': targets['dataset_id'],
            'workspace_id': targets.get('workspace_id'), 'selected_rows': targets['selected_rows'],
            'unique_asins': targets['unique_asins'], 'matrix_enabled': False, 'runs': created}


@router.get('/runs')
def list_runs(limit: int = 50, cursor: str | None = None, hidden: bool = False):
    runs, next_cursor = registry.list_runs(limit=limit, cursor=cursor, hidden=hidden)
    for index, item in enumerate(runs):
        runs[index] = item = registry.public_summary(item)
        item['queue_position'] = registry.queue_position(item['run_id'])
        item.setdefault('attempt', 0)
        item.setdefault('progress', {'stage': item.get('stage') or '', 'done': None,
                                     'total': None, 'unit': None, 'message': item.get('message') or ''})
    return {'runs': runs, 'next_cursor': next_cursor}


def _queue_response():
    state = registry.queue_state()
    items = []
    for index, run_id in enumerate(state['items'], start=1):
        run = registry.read(run_id)
        if run:
            run = registry.public_summary(run)
            run['queue_position'] = index if run.get('status') in (registry.QUEUED, registry.RUNNING, registry.CANCELLING) else None
            run.setdefault('attempt', 0)
            run.setdefault('progress', {'stage': run.get('stage') or '', 'done': None,
                                         'total': None, 'unit': None,
                                         'message': run.get('message') or ''})
            run.setdefault('latest_sequence', registry.latest_sequence(run_id))
            items.append(run)
    return {'items': items, 'current': state.get('current'), 'state': state.get('state'),
            'dispatch_enabled': bool(state.get('dispatch_enabled')),
            'last_completed': state.get('last_completed'), 'updated_at': state.get('updated_at')}


@router.get('/queue')
def queue_get():
    return _queue_response()


@router.post('/queue')
def queue_post(body: dict):
    try:
        runner.start_queue((body or {}).get('run_ids') or [])
    except ValueError as exc:
        _refuse(str(exc))
    return _queue_response()


@router.put('/queue/order')
def queue_order(body: dict):
    try:
        registry.reorder((body or {}).get('run_ids') or [])
    except ValueError as exc:
        _refuse(str(exc))
    return _queue_response()


@router.post('/queue/stop')
def queue_stop():
    runner.stop_queue()
    return _queue_response()


@router.post('/queue/continue')
def queue_continue():
    try:
        runner.continue_queue()
    except ValueError as exc:
        _refuse(str(exc))
    return _queue_response()


@router.patch('/runs/{run_id}')
def run_rename(run_id: str, body: dict):
    _snapshot(run_id)
    return registry.rename(run_id, (body or {}).get('display_name'))


@router.patch('/runs/{run_id}/project')
def run_project_link(run_id: str, body: dict):
    state = registry.read(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    workspace_id = str((body or {}).get('workspace_id') or '').strip() or None
    if workspace_id:
        try:
            project_files.public_project(workspace_id)
        except ValueError as exc:
            _refuse(str(exc))
    registry.write(run_id, workspace_id=workspace_id, library_sync_error='')
    linked = project_link.sync_run_artifacts(run_id, workspace_id) if state.get('status') == registry.DONE else []
    return {'run_id': run_id, 'workspace_id': workspace_id,
            'project_artifact_count': len(linked)}


@router.post('/runs/{run_id}/hide')
def run_hide(run_id: str):
    _snapshot(run_id)
    return registry.set_hidden(run_id, True)


@router.post('/runs/{run_id}/unhide')
def run_unhide(run_id: str):
    _snapshot(run_id)
    return registry.set_hidden(run_id, False)


@router.delete('/runs/{run_id}')
def run_recycle(run_id: str):
    _snapshot(run_id)
    return registry.move_to_recycle_bin(run_id)


@router.post('/runs/{run_id}/open-directory')
def run_open_directory(run_id: str):
    _snapshot(run_id)
    return {'path': registry.open_directory(run_id)}


@router.get('/runs/{run_id}')
def run_detail(run_id: str):
    state = _snapshot(run_id)
    events = registry.event_history(run_id)
    state['timeline'] = [event for event in events if event.get('event_type') in
                         ('run_created', 'run_queued', 'run_started', 'run_finished',
                          'run_failed', 'run_cancelled', 'run_skipped', 'stage_started',
                          'stage_finished', 'stage_progress', 'artifact_ready')]
    state['logs'] = [{'sequence': event.get('sequence'),
                      'line': event.get('payload', {}).get('line', '')} for event in events
                     if event.get('event_type') == 'log']
    state['recent_activity'] = events[-20:]
    return state


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
