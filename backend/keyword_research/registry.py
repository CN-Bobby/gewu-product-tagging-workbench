"""关键词调研的运行登记：一个运行一个目录，状态只写这里。

这个工具不是数据集作业，所以它不写数据库、不碰 BatchJob——打标的数据模型归打标自己。
运行目录是运行状态的唯一真相源：run.json 是权威快照，events.jsonl 是 SSE 消费的追加日志，
因此 sequence 的分配只发生在本模块（和 agent/events.py 的纪律一致）。
"""
import json
import os
import re
import threading
from ..db import STORAGE, now, uid

ROOT = STORAGE / 'keyword-research'
QUEUED, RUNNING, CANCELLING, DONE, FAILED, CANCELLED, INTERRUPTED = (
    'queued', 'running', 'cancelling', 'done', 'failed', 'cancelled', 'interrupted')
TERMINAL = frozenset({DONE, FAILED, CANCELLED, INTERRUPTED})
ACTIVE = frozenset({QUEUED, RUNNING, CANCELLING})

_RUN_ID = re.compile(r'[0-9a-f]{32}')
_LOCK = threading.Lock()


def run_dir(run_id):
    """运行目录。run_id 来自 URL，必须先是我们的 id 形状，否则路径可以跳出根目录。"""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('运行编号非法')
    return ROOT / run_id


def _write_json(path, data):
    """tmp + replace：读取端（快照与 SSE）不能看到写了一半的 JSON。"""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def create(input_data, plan):
    run_id = uid()
    path = run_dir(run_id)
    (path / 'raw').mkdir(parents=True)
    (path / 'cache').mkdir()
    (path / 'logs').mkdir()
    (path / 'output').mkdir()
    _write_json(path / 'input.json', input_data)
    _write_json(path / 'plan.json', plan)
    _write_json(path / 'run.json', {
        'run_id': run_id, 'status': QUEUED, 'stage': '', 'message': '',
        'created_at': now(), 'updated_at': now(), 'artifacts': [], 'error': '',
    })
    return run_id


def read(run_id):
    path = run_dir(run_id) / 'run.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def write(run_id, **fields):
    """合并写入状态字段并返回最新快照。"""
    with _LOCK:
        state = read(run_id)
        if state is None:
            raise ValueError('运行不存在')
        state.update(fields)
        state['updated_at'] = now()
        _write_json(run_dir(run_id) / 'run.json', state)
        return state


def list_runs(limit=50):
    if not ROOT.exists():
        return []
    states = []
    for path in ROOT.iterdir():
        if path.is_dir() and (path / 'run.json').exists():
            try:
                states.append(json.loads((path / 'run.json').read_text(encoding='utf-8')))
            except ValueError:
                continue
    states.sort(key=lambda item: item.get('created_at') or '', reverse=True)
    return states[:max(1, min(int(limit), 200))]


def input_of(run_id):
    return json.loads((run_dir(run_id) / 'input.json').read_text(encoding='utf-8'))


def plan_of(run_id):
    return json.loads((run_dir(run_id) / 'plan.json').read_text(encoding='utf-8'))


# --- 事件日志 -------------------------------------------------------------

def append_event(run_id, event_type, payload=None):
    """追加一条事件并返回它。sequence = 行号 + 1，单调无洞，只能在这里分配。"""
    with _LOCK:
        path = run_dir(run_id) / 'events.jsonl'
        sequence = latest_sequence(run_id) + 1
        event = {'run_id': run_id, 'sequence': sequence, 'event_type': str(event_type),
                 'at': now(), 'payload': payload or {}}
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        return event


def read_events(run_id, after_sequence=0, limit=200):
    """sequence > after_sequence 的升序事件，所以 after_sequence=152 从 153 接着读。"""
    path = run_dir(run_id) / 'events.jsonl'
    if not path.exists():
        return []
    after = max(0, int(after_sequence))
    events = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if int(event['sequence']) > after:
            events.append(event)
            if len(events) >= max(1, min(int(limit), 1000)):
                break
    return events


def latest_sequence(run_id):
    path = run_dir(run_id) / 'events.jsonl'
    if not path.exists():
        return 0
    total = sum(1 for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
    return total


# --- 产物 -----------------------------------------------------------------

def artifacts(run_id):
    state = read(run_id)
    return list(state.get('artifacts') or []) if state else []


def artifact_path(run_id, name):
    """产物只允许取 output/ 下、且已登记在 run.json 里的文件名。"""
    if not isinstance(name, str) or name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('产物名非法')
    for item in artifacts(run_id):
        if item.get('name') == name:
            path = run_dir(run_id) / 'output' / name
            if not path.is_file():
                raise ValueError('产物文件缺失')
            return path
    raise ValueError('未登记的产物')


# --- 重启恢复 -------------------------------------------------------------

def recover():
    """上次进程消失时还在跑的运行不可能继续，如实标为中断，不假装它还在跑。"""
    for state in list_runs(limit=200):
        if state.get('status') not in ACTIVE:
            continue
        write(state['run_id'], status=INTERRUPTED, error='工作台重启，运行已中断')
        append_event(state['run_id'], 'run_failed', {'error': '工作台重启，运行已中断'})
