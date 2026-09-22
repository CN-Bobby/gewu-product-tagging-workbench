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

_LOCK = threading.Lock()
RUNNING = {}  # run_id -> (threading.Event, threading.Thread)


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
    if state['status'] not in registry.ACTIVE:
        raise ValueError('该运行已结束，无需取消')
    _event_of(run_id).set()
    registry.write(run_id, status=registry.CANCELLING, message='正在取消')
    return registry.CANCELLING


def stop_all():
    """工作台退出时取消仍在跑的运行；期间已经结束的不再报错。"""
    for run_id in list(RUNNING):
        try:
            cancel(run_id)
        except ValueError:
            pass


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
        registry.write(self.run_id, stage=phase, message=message)
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
    calls = {'done': 0}

    def emit(event):
        item = dict(event or {})
        kind = str(item.pop('kind', '') or '')
        if kind == 'phase':
            return
        if kind == 'call_success':
            calls['done'] += 1
            registry.append_event(run_id, 'stage_progress',
                                  {'stage': run_log.phase, 'done': calls['done'],
                                   'total': planned_total, 'unit': 'mcp_call',
                                   'message': _line_of(kind, item)})
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
    registry.write(run_id, status=registry.RUNNING, error='')
    try:
        base_url, key, model = credentials.model_target()
        config = EngineConfig(mcp_endpoint=credentials.mcp_endpoint(), llm_base_url=base_url,
                              llm_api_key=key, llm_model=model)
        # 引擎版本与使用的模型身份冻结进运行记录；密钥一律不落盘。
        registry.write(run_id, engine=digest, model={'base_url': base_url, 'model': model})
        registry.append_event(run_id, 'run_started',
                              {'engine_sha256': digest['sha256'], 'planned_calls': planned_total})
        pipeline = Pipeline(config, registry.run_dir(run_id), run_log,
                            on_event=_emitter(run_id, run_log, planned_total),
                            cancel_event=cancel_event)
        result = pipeline.run()
    except Exception as exc:  # noqa: BLE001
        run_log.finish()
        registry.write(run_id, status=registry.FAILED, error=str(exc)[:500])
        registry.append_event(run_id, 'run_failed', {'error': str(exc)[:500]})
        raise
    run_log.finish()
    if str((result or {}).get('status')) == 'canceled':
        registry.write(run_id, status=registry.CANCELLED, message='已取消')
        registry.append_event(run_id, 'run_cancelled', {})
        return {'status': registry.CANCELLED}
    artifacts = _artifacts(run_id)
    # 模板是报告的直接输入，连同引擎资产一起冻结进运行记录
    registry.write(run_id, status=registry.DONE, artifacts=artifacts, message='任务完成',
                   report_template=engine_runner.template_digest())
    for item in artifacts:
        registry.append_event(run_id, 'artifact_ready', item)
    registry.append_event(run_id, 'run_finished', {'artifacts': [x['name'] for x in artifacts]})
    return {'status': registry.DONE, 'artifacts': artifacts}
