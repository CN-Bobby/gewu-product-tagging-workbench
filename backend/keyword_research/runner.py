"""关键词调研的作业执行：把一次运行从 queued 推到终态，并把过程如实记下来。

取消用一个 threading.Event 贯穿：runner 持有它，pipeline 直接改用同一个对象，因此
HTTP 线程上的取消与流水线自己的检查点看到的是同一个事实，不需要中间桥接线程。
"""
import hashlib
import json
import threading

from . import credentials, engine_runner, registry
from .config import EngineConfig
from .pipeline import Pipeline

_LOCK = threading.RLock()
RUNNING = {}  # run_id -> (threading.Event, threading.Thread)
_SCHEDULER_LOCK = threading.Lock()
SHUTTING_DOWN = set()
_SHUTDOWN_EVENT_LOCK = threading.Lock()
_SHUTDOWN_EVENT_GUARDS = {}


def _mark_interrupted(run_id, message='工作台正在关闭，运行已中断'):
    with _SHUTDOWN_EVENT_LOCK:
        guard = _SHUTDOWN_EVENT_GUARDS.setdefault(run_id, threading.Lock())
    with guard:
        with _LOCK:
            state = registry.read(run_id)
            if state is None or state.get('status') in registry.TERMINAL:
                return
            if state.get('status') == registry.INTERRUPTED and state.get('error') == message:
                return
            registry.write(run_id, status=registry.INTERRUPTED, error=message)
            registry.append_event(run_id, 'run_failed', {'error': message})


def _event_of(run_id):
    with _LOCK:
        item = RUNNING.get(run_id)
    if item is None:
        raise ValueError('该运行不在本进程中执行，无法取消')
    return item[0]


def cancel(run_id):
    state = registry.read(run_id)
    if state is None:
        raise ValueError('运行不存在')
    if state['status'] == registry.QUEUED and run_id not in RUNNING:
        registry.write(run_id, status=registry.CANCELLED, message='已取消')
        registry.remove_from_queue(run_id)
        registry.append_event(run_id, 'run_cancelled', {})
        start_dispatch()
        return registry.CANCELLED
    if state['status'] not in registry.ACTIVE:
        raise ValueError('该运行已结束，无需取消')
    _event_of(run_id).set()
    registry.write(run_id, status=registry.CANCELLING, message='正在取消')
    return registry.CANCELLING


def stop_all():
    """工作台退出时标记当前项中断，关闭派发并保留待执行顺序。"""
    queue = registry.queue_state()
    queue['dispatch_enabled'] = False
    queue['state'] = 'stopped' if queue.get('items') else 'idle'
    registry._save_queue(queue)
    with _LOCK:
        running = list(RUNNING.items())
        for run_id, (event, _) in running:
            SHUTTING_DOWN.add(run_id)
            event.set()
    for run_id, _ in running:
        state = registry.read(run_id)
        if state and state.get('status') in registry.ACTIVE:
            _mark_interrupted(run_id)
    queue = registry.queue_state()
    queue['current'] = None
    queue['dispatch_enabled'] = False
    queue['state'] = 'stopped' if queue.get('items') else 'idle'
    registry._save_queue(queue)


def start_dispatch():
    """最多领取并启动一个任务；任务终态后由 finally 再领取下一项。"""
    with _SCHEDULER_LOCK:
        run_id = registry.claim_next()
        if run_id is None:
            return None
        try:
            launch(run_id)
        except Exception:
            registry.finish_current(run_id)
            raise
        return run_id


def start_queue(run_ids):
    state = registry.enqueue(run_ids, enable_dispatch=True)
    start_dispatch()
    return state


def continue_queue():
    state = registry.continue_queue()
    start_dispatch()
    return state


def stop_queue():
    state = registry.stop_queue()
    current = state.get('current')
    if current and current in RUNNING:
        try:
            cancel(current)
        except ValueError:
            pass
    return state


def launch(run_id):
    event = threading.Event()
    thread = threading.Thread(target=_guarded, args=(run_id, event), daemon=True,
                              name='keyword-run-' + run_id[:8])
    with _LOCK:
        if run_id in RUNNING:
            raise ValueError('该运行已在执行')
        RUNNING[run_id] = (event, thread)
    thread.start()
    return thread


def _guarded(run_id, event):
    """线程体只负责不让异常逃逸：状态一律由 execute 自己写到终态。"""
    try:
        execute(run_id, event)
    except Exception:  # noqa: BLE001
        pass
    finally:
        with _LOCK:
            RUNNING.pop(run_id, None)
            stopping = run_id in SHUTTING_DOWN
            SHUTTING_DOWN.discard(run_id)
        if stopping:
            queue = registry.queue_state()
            if queue.get('current') == run_id:
                queue['current'] = None
            queue['dispatch_enabled'] = False
            queue['state'] = 'stopped'
            registry._save_queue(queue)
        else:
            registry.finish_current(run_id)
            try:
                start_dispatch()
            except Exception:
                pass


class _RunLog:
    """Pipeline 只用 log / set_phase 两个方法；它们直接写运行目录，不另开一份状态文件。

    阶段事件也在这里落：on_event 里同样会来一条 phase，若两处都发就变成两条真相。
    """

    def __init__(self, run_id):
        self.run_id = run_id
        self.phase = ''

    def set_phase(self, task_dir, phase, message=''):
        if self.phase and self.phase != phase:
            registry.append_event(self.run_id, 'stage_finished', {'stage': self.phase})
        self.phase = phase
        registry.write(self.run_id, stage=phase, message=message,
                       progress={'stage': phase, 'done': None, 'total': None,
                                 'unit': None, 'message': message})
        registry.append_event(self.run_id, 'stage_started', {'stage': phase, 'message': message})

    def log(self, task_dir, line):
        registry.append_event(self.run_id, 'log', {'line': str(line)})

    def finish(self):
        if self.phase:
            registry.append_event(self.run_id, 'stage_finished', {'stage': self.phase})
            self.phase = ''


def _line_of(kind, payload):
    """把引擎事件渲染成一行中文；只描述事件里已有的字段，不补造。"""
    tool = payload.get('tool') or ''
    sequence = payload.get('seq')
    if kind == 'call_start':
        return f'取数 {sequence}: {tool}（{payload.get("scope") or ""}）'
    if kind == 'call_success':
        text = f'取数 {sequence} 完成: {tool} → {payload.get("returned")} 条'
        return text + '（结果被截断）' if payload.get('truncated') else text
    if kind == 'call_error':
        return f'取数 {sequence} 失败: {tool} — {payload.get("error")}'
    if kind == 'call_retry':
        return f'取数 {sequence} 重试第 {payload.get("attempt")} 次'
    if kind == 'rate_limit_wait':
        return f'限流等待 {payload.get("wait_seconds")} 秒（{tool}）'
    if kind == 'rate_limit_passed':
        return f'限流等待结束，已等 {payload.get("waited_seconds")} 秒'
    if kind == 'pipeline_error':
        return f'流水线错误: {payload.get("error")}'
    return f'{kind}: ' + json.dumps(payload, ensure_ascii=False)


def _emitter(run_id, run_log, planned_total):
    """引擎事件 → 运行事件。阶段事件由 _RunLog 落，这里跳过以免重复。"""
    calls = {'done': 0, 'labels_done': 0, 'label_total': None}

    def emit(event):
        item = dict(event or {})
        kind = str(item.pop('kind', '') or '')
        if kind == 'phase':
            return
        if kind == 'call_success':
            calls['done'] += 1
            payload = {'stage': run_log.phase, 'done': calls['done'],
                       'total': None, 'unit': 'mcp_call',
                       'message': _line_of(kind, item)}
            registry.write(run_id, progress=payload)
            registry.append_event(run_id, 'stage_progress', payload)
            return
        if kind == 'label_batch_done':
            calls['labels_done'] += 1
            payload = {'stage': 'labeling', 'done': calls['labels_done'],
                       'total': calls['label_total'], 'unit': 'label_batch',
                       'message': f"标注批次 {item.get('batch_index')} 完成（{item.get('size')} 个关键词）"}
            registry.write(run_id, progress=payload)
            registry.append_event(run_id, 'stage_progress', payload)
            registry.append_event(run_id, 'log', {'line': payload['message']})
            return
        if kind == 'label_batch_start':
            calls['label_total'] = item.get('total_batches')
            registry.append_event(run_id, 'log', {'line': f"开始标注批次 {item.get('batch_index')} / {calls['label_total']}"})
            return
        if kind.startswith('analysis_segment_'):
            registry.append_event(run_id, 'log', {'line': f"{kind}: " +
                                                  json.dumps(item, ensure_ascii=False)})
            return
        registry.append_event(run_id, 'log', {'line': _line_of(kind, item)})

    return emit


def _artifacts(run_id):
    out = registry.run_dir(run_id) / 'output'
    items = []
    for path in sorted(out.glob('*.xlsx')) if out.is_dir() else []:
        data = path.read_bytes()
        items.append({'name': path.name, 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest()})
    return items


def execute(run_id, cancel_event=None):
    """运行一次。测试可直接在本线程调用它，不必起线程。"""
    state = registry.read(run_id)
    if state is None:
        raise ValueError('运行不存在')
    cancel_event = cancel_event if cancel_event is not None else _event_of(run_id)
    task_input = registry.input_of(run_id)
    plan = registry.plan_of(run_id)
    planned_total = int((plan.get('budget') or {}).get('planned_total') or 0)
    digest = engine_runner.asset_digest()
    run_log = _RunLog(run_id)
    attempt = int(state.get('attempt') or 0) + 1
    registry.write(run_id, status=registry.RUNNING, error='', attempt=attempt)
    try:
        selection = plan.get('selection') or {}
        base_url, key, model = credentials.model_target(selection.get('model_ref'),
                                                        selection.get('credential_id'))
        config = EngineConfig(mcp_endpoint=credentials.mcp_endpoint(), llm_base_url=base_url,
                              llm_api_key=key, llm_model=model)
        # 引擎版本与使用的模型身份冻结进运行记录；密钥一律不落盘。
        model_snapshot = {'base_url': base_url, 'model': model}
        if selection.get('model_ref'):
            model_snapshot['model_ref'] = selection['model_ref']
        if selection.get('credential_id'):
            model_snapshot['credential_id'] = selection['credential_id']
        registry.write(run_id, engine=digest, model=model_snapshot)
        registry.append_event(run_id, 'run_started',
                              {'engine_sha256': digest['sha256'], 'planned_calls': planned_total,
                               'attempt': attempt})
        pipeline = Pipeline(config, registry.run_dir(run_id), run_log,
                            on_event=_emitter(run_id, run_log, planned_total),
                            cancel_event=cancel_event)
        result = pipeline.run()
    except Exception as exc:  # noqa: BLE001
        run_log.finish()
        stopping = run_id in SHUTTING_DOWN
        message = '工作台正在关闭，运行已中断' if stopping else str(exc)[:500]
        if stopping:
            _mark_interrupted(run_id, message)
        else:
            with _LOCK:
                if run_id in SHUTTING_DOWN:
                    _mark_interrupted(run_id)
                else:
                    registry.write(run_id, status=registry.FAILED, error=message)
                    registry.append_event(run_id, 'run_failed', {'error': message})
        raise
    run_log.finish()
    if run_id in SHUTTING_DOWN:
        message = '工作台正在关闭，运行已中断'
        _mark_interrupted(run_id, message)
        return {'status': registry.INTERRUPTED}
    if str((result or {}).get('status')) == 'canceled':
        with _LOCK:
            if run_id in SHUTTING_DOWN:
                _mark_interrupted(run_id)
                return {'status': registry.INTERRUPTED}
            registry.write(run_id, status=registry.CANCELLED, message='已取消')
            registry.append_event(run_id, 'run_cancelled', {})
        return {'status': registry.CANCELLED}
    artifacts = _artifacts(run_id)
    # 模板是报告的直接输入，连同引擎资产一起冻结进运行记录
    template = engine_runner.template_digest()
    with _LOCK:
        if run_id in SHUTTING_DOWN:
            _mark_interrupted(run_id)
            return {'status': registry.INTERRUPTED}
        registry.write(run_id, status=registry.DONE, artifacts=artifacts, message='任务完成',
                       report_template=template)
    for item in artifacts:
        registry.append_event(run_id, 'artifact_ready', item)
    registry.append_event(run_id, 'run_finished', {'artifacts': [x['name'] for x in artifacts]})
    return {'status': registry.DONE, 'artifacts': artifacts}
