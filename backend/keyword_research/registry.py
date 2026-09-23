"""关键词任务快照、单调事件流和持久队列元数据。

每个任务仍以独立目录为存储单位；queue.json 与运行目录并列，采用临时文件加原子替换，
记录排队顺序、当前项和派发开关。密钥永远不写入这里。
"""
import json
import os
import re
import threading
import time
from ..db import STORAGE, now, uid

ROOT = STORAGE / 'keyword-research'
CREATED, QUEUED, RUNNING, CANCELLING, DONE, FAILED, CANCELLED, INTERRUPTED, SKIPPED = (
    'created', 'queued', 'running', 'cancelling', 'done', 'failed', 'cancelled', 'interrupted', 'skipped')
TERMINAL = frozenset({DONE, FAILED, CANCELLED, INTERRUPTED, SKIPPED})
ACTIVE = frozenset({QUEUED, RUNNING, CANCELLING})
_RUN_ID = re.compile(r'[0-9a-f]{32}')
_LOCK = threading.RLock()
_READ_RETRIES = 6


def _read_text_retry(path, encoding='utf-8'):
    """Retry brief Windows sharing violations while an atomic replace is in flight."""
    for attempt in range(_READ_RETRIES):
        try:
            return path.read_text(encoding=encoding)
        except PermissionError:
            if attempt + 1 >= _READ_RETRIES:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.05))


def _replace_retry(source, destination):
    """Retry transient Windows sharing violations while a snapshot is being read."""
    for attempt in range(_READ_RETRIES):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= _READ_RETRIES:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.05))


class QueueStateCorrupt(ValueError):
    """Persistent queue metadata is unreadable; dispatch must fail closed."""


def run_dir(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('运行编号非法')
    candidate = ROOT / run_id
    is_junction = getattr(candidate, 'is_junction', lambda: False)
    if candidate.is_symlink() or is_junction():
        raise ValueError('运行目录不能是符号链接或目录联接')
    if candidate.parent.resolve() != ROOT.resolve():
        raise ValueError('运行目录越过关键词数据根目录')
    return candidate


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    _replace_retry(tmp, path)


def queue_path():
    return ROOT / 'queue.json'


def _default_queue():
    return {'items': [], 'current': None, 'state': 'idle', 'dispatch_enabled': False,
            'updated_at': now(), 'last_completed': None}


def queue_state():
    with _LOCK:
        path = queue_path()
        if not path.exists():
            return _default_queue()
        try:
            value = json.loads(_read_text_retry(path))
        except (OSError, ValueError) as exc:
            raise QueueStateCorrupt('关键词队列状态文件损坏，已停止派发；请备份并检查 keyword-research/queue.json') from exc
        if (not isinstance(value, dict) or not isinstance(value.get('items'), list)
                or not isinstance(value.get('dispatch_enabled'), bool)
                or value.get('state') not in ('idle', 'running', 'stopped')
                or any(not isinstance(x, str) or not _RUN_ID.fullmatch(x) for x in value['items'])):
            raise QueueStateCorrupt('关键词队列状态结构无效，已停止派发；请备份并检查 keyword-research/queue.json')
        state = _default_queue()
        state.update(value)
        if state.get('current') and not _RUN_ID.fullmatch(str(state['current'])):
            state['current'] = None
        return state


def _save_queue(state):
    with _LOCK:
        state['updated_at'] = now()
        _write_json(queue_path(), state)
        return state


def create(input_data, plan, display_name=None, dispatch='create'):
    run_id = uid()
    path = run_dir(run_id)
    (path / 'raw').mkdir(parents=True)
    (path / 'cache').mkdir()
    (path / 'logs').mkdir()
    (path / 'output').mkdir()
    _write_json(path / 'input.json', input_data)
    _write_json(path / 'plan.json', plan)
    stamp = now()
    _write_json(path / 'run.json', {
        'run_id': run_id, 'status': CREATED, 'stage': '', 'message': '',
        'created_at': stamp, 'updated_at': stamp, 'display_name': str(display_name or '').strip()[:120],
        'artifacts': [], 'error': '', 'attempt': 0,
        'progress': {'stage': '', 'done': None, 'total': None, 'unit': None, 'message': ''},
        'latest_sequence': 0, 'hidden': False,
    })
    if dispatch == 'enqueue':
        enqueue([run_id], enable_dispatch=True)
    return run_id


def read(run_id):
    path = run_dir(run_id) / 'run.json'
    if not path.exists():
        return None
    return json.loads(_read_text_retry(path))


def write(run_id, **fields):
    with _LOCK:
        state = read(run_id)
        if state is None:
            raise ValueError('运行不存在')
        state.update(fields)
        state['updated_at'] = now()
        _write_json(run_dir(run_id) / 'run.json', state)
        return state


def list_runs(limit=50, cursor=None, hidden=False):
    if not ROOT.exists():
        return [], None
    states = []
    for path in ROOT.iterdir():
        if not path.is_dir() or not _RUN_ID.fullmatch(path.name) or not (path / 'run.json').exists():
            continue
        try:
            item = json.loads(_read_text_retry(path / 'run.json'))
        except (OSError, ValueError):
            continue
        if bool(item.get('hidden')) != bool(hidden):
            continue
        states.append(item)
    states.sort(key=lambda item: (item.get('created_at') or '', item.get('run_id') or ''), reverse=True)
    if cursor:
        states = [item for item in states if (item.get('created_at') or '') + ':' + item.get('run_id', '') < str(cursor)]
    try:
        size = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        size = 50
    page = states[:size]
    next_cursor = None
    if len(states) > size and page:
        last = page[-1]
        next_cursor = (last.get('created_at') or '') + ':' + last.get('run_id', '')
    # The execution/scheduling view may request all tasks; pagination applies only to HTTP listing.
    return page, next_cursor


def all_runs():
    if not ROOT.exists():
        return []
    items = []
    for path in ROOT.iterdir():
        if path.is_dir() and _RUN_ID.fullmatch(path.name) and (path / 'run.json').is_file():
            try:
                items.append(json.loads(_read_text_retry(path / 'run.json')))
            except (OSError, ValueError):
                continue
    return items


def input_of(run_id):
    return json.loads(_read_text_retry(run_dir(run_id) / 'input.json'))


def plan_of(run_id):
    return json.loads(_read_text_retry(run_dir(run_id) / 'plan.json'))


def public_summary(state):
    """Attach safe list facts without exposing ASINs, Node IDs, or raw task input."""
    run_id = state.get('run_id')
    try:
        groups = input_of(run_id).get('groups') or []
        plan = plan_of(run_id)
    except (OSError, ValueError, TypeError):
        groups, plan = [], {}
    summary = {'group_count': len(groups),
               'asin_count': sum(len(group.get('asins') or []) for group in groups),
               'node_count': sum(len(group.get('node_ids') or []) for group in groups),
               'marketplaces': list(dict.fromkeys(group.get('marketplace') for group in groups
                                                   if group.get('marketplace')))}
    summary['marketplace'] = summary['marketplaces'][0] if len(summary['marketplaces']) == 1 else None
    result = dict(state)
    result['input_summary'] = summary
    result.update(summary)
    result['budget'] = plan.get('budget') or {}
    return result


def queue_position(run_id):
    state = queue_state()
    try:
        return state['items'].index(run_id) + 1 if run_id in state['items'] else None
    except (TypeError, ValueError):
        return None


def enqueue(run_ids, enable_dispatch=False):
    ids = list(dict.fromkeys(run_ids or []))
    if not ids:
        raise ValueError('请选择至少一个关键词任务')
    for run_id in ids:
        run_dir(run_id)
        state = read(run_id)
        if state is None:
            raise ValueError('运行不存在：' + run_id)
        if state.get('status') not in (CREATED, FAILED, CANCELLED, INTERRUPTED, SKIPPED):
            raise ValueError('任务当前状态不可重新排队：' + str(state.get('display_name') or run_id))
    with _LOCK:
        queue = queue_state()
        for run_id in ids:
            if run_id in queue['items']:
                queue['items'].remove(run_id)
            state = read(run_id)
            attempt = int(state.get('attempt') or 0)
            write(run_id, status=QUEUED, error='', message='等待队列执行')
            queue['items'].append(run_id)
            append_event(run_id, 'run_queued', {'attempt': attempt,
                                               'next_attempt': attempt + 1,
                                               'queue_position': len(queue['items'])})
        if enable_dispatch:
            queue['dispatch_enabled'] = True
        queue['state'] = 'running' if queue.get('dispatch_enabled') else 'stopped'
        _save_queue(queue)
    return queue_state()


def reorder(run_ids):
    ids = list(dict.fromkeys(run_ids or []))
    if any(not isinstance(x, str) or not _RUN_ID.fullmatch(x) for x in ids):
        raise ValueError('运行编号非法')
    with _LOCK:
        queue = queue_state()
        pinned = [x for x in queue['items'] if x == queue.get('current') or (read(x) or {}).get('status') != QUEUED]
        reorderable = [x for x in queue['items'] if x not in pinned]
        if set(ids) != set(reorderable):
            raise ValueError('顺序必须包含所有待执行任务且不能包含当前或已结束任务')
        pos = 0
        output = []
        for item in queue['items']:
            if item in pinned:
                output.append(item)
            else:
                output.append(ids[pos]); pos += 1
        queue['items'] = output
        _save_queue(queue)
    return queue_state()


def claim_next():
    with _LOCK:
        queue = queue_state()
        if not queue['dispatch_enabled'] or queue.get('current'):
            return None
        for run_id in queue['items']:
            state = read(run_id)
            if state and state.get('status') == QUEUED:
                queue['current'] = run_id
                queue['state'] = 'running'
                _save_queue(queue)
                return run_id
        queue['state'] = 'idle'
        queue['dispatch_enabled'] = False
        _save_queue(queue)
        return None


def finish_current(run_id):
    with _LOCK:
        queue = queue_state()
        if queue.get('current') == run_id:
            queue['current'] = None
        queue['last_completed'] = run_id
        _save_queue(queue)


def stop_queue():
    with _LOCK:
        queue = queue_state()
        current = queue.get('current')
        queue['dispatch_enabled'] = False
        queue['state'] = 'stopped'
        for run_id in queue['items']:
            if run_id == current:
                continue
            state = read(run_id)
            if state and state.get('status') == QUEUED:
                write(run_id, status=SKIPPED, message='队列已停止，任务已跳过')
                append_event(run_id, 'run_skipped', {'reason': 'queue_stopped'})
        _save_queue(queue)
    return queue


def continue_queue():
    with _LOCK:
        queue = queue_state()
        if queue.get('current'):
            raise ValueError('队列中仍有运行中的任务')
        for run_id in queue['items']:
            state = read(run_id)
            if state and state.get('status') == INTERRUPTED:
                write(run_id, status=QUEUED, message='等待继续执行')
                append_event(run_id, 'run_queued', {'attempt': int(state.get('attempt') or 0), 'resume': True})
        queue['dispatch_enabled'] = True
        queue['state'] = 'running'
        _save_queue(queue)
    return queue_state()


def recover():
    """重启后不派发任务；当前运行变 interrupted，未开始的 queued 项保留待手动继续。"""
    with _LOCK:
        queue = queue_state()
        active = [s for s in all_runs() if s.get('status') in (RUNNING, CANCELLING)]
        persisted_current = queue.get('current')
        if persisted_current and persisted_current not in queue['items']:
            queue['items'].insert(0, persisted_current)
        queue['current'] = None
        for state in active:
            run_id = state['run_id']
            write(run_id, status=INTERRUPTED, error='工作台重启，运行已中断')
            append_event(run_id, 'run_failed', {'error': '工作台重启，运行已中断'})
            if run_id not in queue['items']:
                queue['items'].insert(0, run_id)
        # Legacy queued records were previously dispatched without queue metadata; preserve them.
        for state in all_runs():
            if state.get('status') == QUEUED and state['run_id'] not in queue['items']:
                queue['items'].append(state['run_id'])
        queue['dispatch_enabled'] = False
        queue['state'] = 'stopped' if queue['items'] else 'idle'
        _save_queue(queue)


def rename(run_id, display_name):
    name = str(display_name or '').strip()
    if not name or len(name) > 120:
        raise ValueError('任务名称需为 1 至 120 个字符')
    if read(run_id) is None:
        raise ValueError('运行不存在')
    return write(run_id, display_name=name)


def set_hidden(run_id, hidden):
    if read(run_id) is None:
        raise ValueError('运行不存在')
    return write(run_id, hidden=bool(hidden))


def remove_from_queue(run_id):
    with _LOCK:
        queue = queue_state()
        queue['items'] = [x for x in queue['items'] if x != run_id]
        if queue.get('current') == run_id:
            queue['current'] = None
        _save_queue(queue)


def open_directory(run_id):
    path = run_dir(run_id).resolve()
    root = ROOT.resolve()
    if path.parent != root or path.is_symlink() or not path.is_dir():
        raise ValueError('任务目录无效')
    if os.name != 'nt':
        raise OSError('仅 Windows 工作台支持打开本地任务目录')
    os.startfile(str(path))
    return str(path)


def move_to_recycle_bin(run_id):
    """调用 Windows Shell 回收站；不支持/失败时抛错，绝不改为永久删除。"""
    state = read(run_id)
    if state is None:
        raise ValueError('运行不存在')
    if state.get('status') in ACTIVE or queue_state().get('current') == run_id:
        raise ValueError('运行中的任务不可移入回收站')
    path = run_dir(run_id).resolve()
    root = ROOT.resolve()
    if path.parent != root or path.is_symlink() or not path.is_dir():
        raise ValueError('任务目录路径校验失败')
    if os.name != 'nt':
        raise OSError('当前平台不支持 Windows 回收站操作')
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', wintypes.LPVOID), ('lpszProgressTitle', wintypes.LPCWSTR)]

    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO = 0x0004, 0x0010, 0x0040
    source_path = ctypes.create_unicode_buffer(str(path) + '\0\0')
    operation = SHFILEOPSTRUCTW(None, 3, ctypes.cast(source_path, wintypes.LPCWSTR), None,
                                FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO,
                                False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f'移入 Windows 回收站失败（错误码 {result}）')
    remove_from_queue(run_id)
    return {'run_id': run_id, 'recycled': True}


def append_event(run_id, event_type, payload=None):
    with _LOCK:
        path = run_dir(run_id) / 'events.jsonl'
        sequence = latest_sequence(run_id) + 1
        event = {'run_id': run_id, 'sequence': sequence, 'event_type': str(event_type),
                 'at': now(), 'payload': payload or {}}
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        state_path = run_dir(run_id) / 'run.json'
        if state_path.exists():
            state = json.loads(_read_text_retry(state_path))
            state['latest_sequence'] = sequence
            state['updated_at'] = now()
            _write_json(state_path, state)
        return event


def read_events(run_id, after_sequence=0, limit=200):
    path = run_dir(run_id) / 'events.jsonl'
    if not path.exists():
        return []
    after = max(0, int(after_sequence))
    events = []
    for line in _read_text_retry(path).splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if int(event['sequence']) > after:
            events.append(event)
            if len(events) >= max(1, min(int(limit), 10000)):
                break
    return events


def event_history(run_id):
    """Read the full durable event trail for detail snapshots and refresh recovery."""
    path = run_dir(run_id) / 'events.jsonl'
    if not path.exists():
        return []
    events = []
    for line in _read_text_retry(path).splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def latest_sequence(run_id):
    path = run_dir(run_id) / 'events.jsonl'
    if not path.exists():
        return 0
    return sum(1 for line in _read_text_retry(path).splitlines() if line.strip())


def artifacts(run_id):
    state = read(run_id)
    return list(state.get('artifacts') or []) if state else []


def artifact_path(run_id, name):
    if not isinstance(name, str) or name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('产物名非法')
    for item in artifacts(run_id):
        if item.get('name') == name:
            path = run_dir(run_id) / 'output' / name
            if not path.is_file():
                raise ValueError('产物文件缺失')
            return path
    raise ValueError('未登记的产物')
