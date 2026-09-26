"""One serial, in-process Sorftime worker over the vendored xlj_core state machine."""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import settings
from .sorftime import SorftimeClient, call_sync, scrub

_LOCK = threading.RLock()
_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: threading.Thread | None = None
_ACTIVE_RUN: str | None = None
_QUEUE: list[str] = []
_RESCHEDULE: set[str] = set()
_BUSY_RUNS: set[str] = set()
_CALLING_RUNS: set[str] = set()
_RECOVERY_LOCK = threading.RLock()
_RECOVERY_WARNINGS: list[dict[str, str]] = []
_CLIENT_FACTORY = SorftimeClient
_RUN_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_LOGGER = logging.getLogger(__name__)


def root() -> Path:
    from ..db import STORAGE
    return STORAGE / 'xunlongjue'


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('运行编号非法')
    return run_id


def _safe_run_dir(run_id: str) -> Path:
    run_id = _validate_run_id(run_id)
    base = root().resolve()
    candidate = base / run_id
    is_junction = getattr(candidate, 'is_junction', lambda: False)
    if candidate.is_symlink() or is_junction() or candidate.resolve().parent != base:
        raise ValueError('运行目录路径无效')
    return candidate


def _workspace(run_id: str) -> Path:
    run_dir = _safe_run_dir(run_id)
    path = run_dir / 'workspace'
    is_junction = getattr(path, 'is_junction', lambda: False)
    if path.is_symlink() or is_junction() or path.resolve().parent != run_dir.resolve():
        raise ValueError('工作区路径无效')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_root(run_id: str) -> Path:
    return _safe_run_dir(run_id)


def _control_path(run_id: str) -> Path:
    return _run_root(run_id) / 'control.json'


def _read_control(run_id: str) -> dict:
    try:
        value = json.loads(_control_path(run_id).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_control(run_id: str, **fields) -> dict:
    path = _control_path(run_id)
    state = _read_control(run_id)
    state.update(fields)
    state['updated_at'] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)
    return state


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _engine():
    """Import vendored source only; never resolve the installed xlj_core package."""
    return importlib.import_module('.vendor.xlj_core.engine', package=__package__)


def _outputs():
    return importlib.import_module('.vendor.xlj_core.outputs', package=__package__)


def start() -> None:
    """App lifespan hook. Start idle coordinator only; paid work is never resumed here."""
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker_loop, name='xunlongjue-worker', daemon=True)
        _THREAD.start()


def recover() -> None:
    """Mark active runs interrupted on startup. Explicit resume is required to call MCP again."""
    with _RECOVERY_LOCK:
        _RECOVERY_WARNINGS.clear()
        try:
            base = root()
            if not base.is_dir():
                return
            entries = iter(base.iterdir())
        except Exception as exc:  # noqa: BLE001 - recovery must never prevent app startup.
            _add_recovery_warning('', 'directory_scan_failed', exc)
            return

        while True:
            try:
                run_dir = next(entries)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - a directory read can fail mid-iteration.
                _add_recovery_warning('', 'directory_scan_failed', exc)
                break

            run_id = run_dir.name
            # Ignore unrelated directories and unsafe links. They are not run records.
            if not _RUN_ID.fullmatch(run_id):
                continue
            try:
                safe_dir = _safe_run_dir(run_id)
                if not safe_dir.is_dir():
                    continue
                control_path = safe_dir / 'control.json'
                try:
                    control = json.loads(control_path.read_text(encoding='utf-8'))
                except FileNotFoundError:
                    continue
                except json.JSONDecodeError as exc:
                    _add_recovery_warning(run_id, 'invalid_control', exc)
                    continue
                except (OSError, UnicodeError) as exc:
                    _add_recovery_warning(run_id, 'read_failed', exc)
                    continue
                if not isinstance(control, dict):
                    _add_recovery_warning(run_id, 'invalid_control', ValueError())
                    continue
                if control.get('status') in ('running', 'paused', 'pausing', 'queued'):
                    try:
                        _write_control(run_id, status='interrupted',
                                       message='程序重启后已停止；需要显式恢复')
                    except Exception as exc:  # noqa: BLE001 - preserve other recoverable runs.
                        _add_recovery_warning(run_id, 'write_failed', exc)
            except ValueError:
                # Includes junctions and paths that fail the existing safety checks.
                continue
            except Exception as exc:  # noqa: BLE001 - isolate unexpected per-record failures.
                _add_recovery_warning(run_id, 'read_failed', exc)


def _add_recovery_warning(run_id: str, reason: str, error: Exception) -> None:
    """Keep recovery diagnostics useful without logging paths, contents, or secrets."""
    safe_id = run_id if _RUN_ID.fullmatch(run_id or '') else ''
    warning = {'run_id': safe_id, 'reason': reason}
    _RECOVERY_WARNINGS.append(warning)
    _LOGGER.warning('寻龙诀历史任务恢复跳过一条记录 reason=%s error_type=%s',
                    reason, type(error).__name__)


def recovery_warnings() -> dict:
    """Return a safe summary for the UI; raw paths and exception messages stay private."""
    with _RECOVERY_LOCK:
        items = [dict(item) for item in _RECOVERY_WARNINGS]
    return {'count': len(items), 'items': items}


def stop_all() -> None:
    global _THREAD
    _STOP.set()
    _WAKE.set()
    with _LOCK:
        active = _ACTIVE_RUN
        queued = list(_QUEUE)
        _QUEUE.clear()
        thread = _THREAD
    if active:
        _write_control(active, status='interrupted', message='工作台关闭，运行已中断')
    for run_id in queued:
        _write_control(run_id, status='interrupted', message='工作台关闭，运行已中断')
    if thread and thread is not threading.current_thread():
        thread.join(timeout=5)
    with _LOCK:
        if not thread or not thread.is_alive():
            _THREAD = None


def _schedule(run_id: str) -> None:
    with _LOCK:
        if run_id == _ACTIVE_RUN:
            _RESCHEDULE.add(run_id)
        elif run_id not in _QUEUE:
            _QUEUE.append(run_id)
        _WAKE.set()


def create_run(site: str, node_id: str, max_depth: int | None = None,
               max_nodes: int | None = None) -> dict:
    endpoint, _ = settings.connection()
    # Validate inexpensive policy inputs before creating persistent state.
    depth = 12 if max_depth is None else int(max_depth)
    if not 1 <= depth <= 100:
        raise ValueError('max_depth 必须在 1 至 100 之间')
    node_cap = None if max_nodes is None else int(max_nodes)
    if node_cap is not None and not 1 <= node_cap <= 100000:
        raise ValueError('max_nodes 必须在 1 至 100000 之间')
    run_id = str(uuid.uuid4())
    run_dir = root() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / 'workspace'
    workspace.mkdir()
    report_dir = run_dir / 'reports'
    report_dir.mkdir()
    engine = _engine()
    policy = {'max_level': depth}
    if node_cap is not None:
        policy['max_nodes'] = node_cap
    engine.create_run(str(workspace), site, str(node_id), str(node_id),
                      policy=policy, report_dir=str(report_dir), run_id=run_id)
    _write_control(run_id, status='running', site=site.upper(), node_id=str(node_id),
                   max_depth=depth, max_nodes=node_cap, message='', report_path='')
    start()
    _schedule(run_id)
    return {'run_id': run_id, 'id': run_id, 'status': 'running', 'reused': False}


def _engine_state(run_id: str) -> dict:
    return _engine().status(str(_workspace(run_id)), run_id)


def list_runs() -> list[dict]:
    base = root()
    if not base.exists():
        return []
    rows = []
    engine = _engine()
    for path in base.iterdir():
        if not path.is_dir() or not (path / 'control.json').exists():
            continue
        try:
            workspace = str(path / 'workspace')
            source_runs = engine.list_runs(workspace)
            if source_runs:
                rows.append(get_run(path.name))
        except (ValueError, OSError):
            continue
    return sorted(rows, key=lambda item: item.get('updated_at', ''), reverse=True)


def get_run(run_id: str) -> dict:
    control = _read_control(run_id)
    if not control:
        raise ValueError('运行不存在')
    state = _engine_state(run_id)
    engine_status = str(state.get('status') or '').upper()
    control_status = str(control.get('status') or '').lower()
    if control_status in ('paused', 'pausing', 'interrupted', 'queued', 'failed'):
        engine_status = control_status.upper()
    state['engine_status'] = str(state.get('status') or '').upper()
    state['status'] = {'ALL_DONE': 'completed', 'PARTIAL_DONE': 'partial_done'}.get(
        engine_status, engine_status.lower())
    state['activity_status'] = (control_status if control_status in ('paused', 'pausing', 'interrupted')
                                else str(state.get('activity_status') or engine_status).lower())
    state['id'] = run_id
    state['node_id'] = state.get('root_node_id') or control.get('node_id')
    state['progress_percent'] = state.get('completion_percent')
    state['progress'] = state.get('completion_percent')
    state.update({key: control.get(key) for key in ('max_depth', 'max_nodes', 'message', 'report_path')})
    report_ready = ((engine_status == 'ALL_DONE' and control.get('report_mode') == 'final')
                    or (engine_status == 'PARTIAL_DONE' and control.get('report_mode') == 'partial'))
    state['report_url'] = f'/api/xunlongjue/runs/{run_id}/report' if report_ready and control.get('report_path') else None
    state['updated_at'] = control.get('updated_at') or state.get('updated_at')
    return state


def home_summary() -> dict:
    configured = settings.status()['key_configured']
    return {'key_configured': configured, 'recent_runs': list_runs()[:5]}


def pause(run_id: str) -> dict:
    get_run(run_id)
    with _LOCK:
        status = 'pausing' if run_id in _BUSY_RUNS else 'paused'
        _write_control(run_id, status=status,
                       message='当前 MCP 请求完成并安全写入后暂停' if status == 'pausing' else '已暂停')
    return get_run(run_id)


def resume(run_id: str) -> dict:
    state = get_run(run_id)
    if state.get('status') not in ('paused', 'interrupted', 'partial_done'):
        raise ValueError('该运行当前不能恢复')
    if state.get('engine_status') == 'ALL_DONE':
        raise ValueError('已全部完成的运行不能恢复')
    # Explicit user resume is the only point at which a stale lease is force-released.
    engine = _engine()
    engine.takeover_run(str(_workspace(run_id)), run_id, force=True,
                        worker_label='格物工作台/显式恢复')
    if state.get('status') == 'partial_done' or state.get('node_counts', {}).get('SUSPENDED'):
        engine.resume_run(str(_workspace(run_id)), run_id, all_suspended=True)
    _write_control(run_id, status='running', message='', report_path='', report_mode='')
    start()
    _schedule(run_id)
    return get_run(run_id)


def report_file(run_id: str) -> Path:
    state = get_run(run_id)
    valid_mode = {'ALL_DONE': 'final', 'PARTIAL_DONE': 'partial'}.get(state.get('engine_status'))
    control = _read_control(run_id)
    if not valid_mode or control.get('report_mode') != valid_mode or not state.get('report_path'):
        raise ValueError('报告仅在 ALL_DONE 或 PARTIAL_DONE 后可用')
    reports_path = _run_root(run_id) / 'reports'
    is_junction = getattr(reports_path, 'is_junction', lambda: False)
    if reports_path.is_symlink() or is_junction():
        raise ValueError('报告目录路径无效')
    reports = reports_path.resolve()
    report = Path(state['report_path']).resolve()
    if report.parent != reports or report.suffix.lower() != '.html' or not report.is_file():
        raise ValueError('报告文件不存在或路径无效')
    return report


def _cap_tree_result(run_id: str, payload):
    control = _read_control(run_id)
    cap = control.get('max_nodes')
    if not cap:
        return payload
    if isinstance(payload, dict):
        keys = [name for name in ('items', 'subcategories', 'data')
                if isinstance(payload.get(name), list)]
        key = next((name for name in keys if payload.get(name)), keys[0] if keys else None)
        if key is None:
            return payload
        raw = payload[key]
    elif isinstance(payload, list):
        key, raw = None, payload
    else:
        return payload
    state = _engine_state(run_id)
    remaining = max(0, int(cap) - int(state.get('total_nodes') or 0))
    clipped = raw[:remaining]
    if key is None:
        return clipped
    copy = dict(payload)
    copy[key] = clipped
    return copy


def _call_for_job(client, job: dict) -> dict:
    kind = job.get('type')
    if kind not in ('category_tree', 'category_trend'):
        raise ValueError('引擎返回未知任务类型')
    arguments = {'nodeId': str(job.get('node_id') or ''),
                 'amzSite': str(job.get('site') or '')}
    if kind == 'category_trend':
        arguments['trendIndex'] = str(job.get('indicator') or '')
    return call_sync(client, kind, arguments)


def _process_batch(run_id: str, batch: dict, client) -> None:
    engine = _engine()
    results = []
    for job in batch.get('jobs') or []:
        with _LOCK:
            control_status = _read_control(run_id).get('status')
            if _STOP.is_set() or control_status in ('paused', 'pausing'):
                dispatch = False
            else:
                # This lock makes the pause request's pre-call boundary atomic: either
                # pause wins and the leased job is returned, or the MCP call is marked active.
                _CALLING_RUNS.add(run_id)
                dispatch = True
        if not dispatch:
            _release_unstarted_batch(run_id, batch)
            _write_control(run_id, status='paused', message='已暂停；未发送的任务已安全退回')
            return
        try:
            payload = _call_for_job(client, job)
            if job.get('type') == 'category_tree':
                payload = _cap_tree_result(run_id, payload)
            result = {'job_id': job['job_id'], 'attempt': job['attempt'],
                      'lease_token': job['lease_token'], 'status': 'success', 'payload': payload}
        except Exception as exc:  # noqa: BLE001
            result = {'job_id': job['job_id'], 'attempt': job['attempt'],
                      'lease_token': job['lease_token'], 'status': 'failed',
                      'error': scrub(exc, client.api_key)}
        finally:
            with _LOCK:
                _CALLING_RUNS.discard(run_id)
        results.append(result)
        engine.pulse_batch(str(_workspace(run_id)), run_id, batch['batch_id'],
                           completed=len(results), failed=sum(x['status'] == 'failed' for x in results),
                           current_node_id=job.get('node_id', ''), stage='INGESTING')
    if len(results) != len(batch.get('jobs') or []):
        return
    document = {'schema_version': batch['schema_version'], 'run_id': run_id,
                'batch_id': batch['batch_id'], 'results': results}
    target = Path(batch['results_draft_path'])
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, target)
    engine.ingest_batch(str(_workspace(run_id)), run_id, batch['batch_id'])


def _release_unstarted_batch(run_id: str, batch: dict) -> None:
    """Return a leased but never-dispatched batch without consuming an attempt."""
    engine = _engine()
    workspace = str(_workspace(run_id))
    engine.takeover_run(workspace, run_id, force=True,
                        worker_label='格物工作台/暂停前退回')
    xlj_db = importlib.import_module('.vendor.xlj_core.db', package=__package__)
    conn = xlj_db.connect(workspace)
    try:
        with xlj_db.transaction(conn):
            for job in batch.get('jobs') or []:
                conn.execute(
                    "UPDATE jobs SET attempts=MAX(0,attempts-1),updated_at=? "
                    "WHERE job_id=? AND state='PENDING' AND attempts=?",
                    (xlj_db.utc_now(), job.get('job_id'), job.get('attempt')),
                )
    finally:
        conn.close()


def _finish_worker_boundary(run_id: str) -> None:
    """Close a pause only after the current lease was ingested and terminal output is staged."""
    with _LOCK:
        pending_pause = _read_control(run_id).get('status') == 'pausing'
    if pending_pause:
        report_path = ''
        report_mode = ''
        try:
            state = _engine_state(run_id)
            terminal = state.get('status')
            if terminal == 'ALL_DONE':
                report_path = _outputs().generate_html(
                    str(_workspace(run_id)), run_id, 'final', str(_run_root(run_id) / 'reports'))
                report_mode = 'final'
                final_status, message = 'all_done', ''
            elif terminal == 'PARTIAL_DONE':
                report_path = _outputs().generate_html(
                    str(_workspace(run_id)), run_id, 'partial', str(_run_root(run_id) / 'reports'))
                report_mode = 'partial'
                final_status, message = 'partial_done', '运行已部分完成'
            else:
                final_status, message = 'paused', '已暂停；当前请求结果已安全写入'
        except Exception as exc:  # noqa: BLE001
            final_status, message = 'interrupted', scrub(exc)
        with _LOCK:
            control = _read_control(run_id)
            if control.get('status') == 'pausing':
                _write_control(run_id, status=final_status, message=message,
                               report_path=report_path, report_mode=report_mode)
            _BUSY_RUNS.discard(run_id)
            _CALLING_RUNS.discard(run_id)
            if run_id in _RESCHEDULE:
                _RESCHEDULE.discard(run_id)
                if _read_control(run_id).get('status') == 'running' and run_id not in _QUEUE:
                    _QUEUE.append(run_id)
            return
    with _LOCK:
        _BUSY_RUNS.discard(run_id)
        _CALLING_RUNS.discard(run_id)
        if run_id in _RESCHEDULE:
            _RESCHEDULE.discard(run_id)
            if _read_control(run_id).get('status') == 'running' and run_id not in _QUEUE:
                _QUEUE.append(run_id)


def _run_one(run_id: str) -> None:
    engine = _engine()
    workspace = str(_workspace(run_id))
    client = None
    while not _STOP.is_set():
        control = _read_control(run_id)
        if control.get('status') != 'running':
            return
        state = engine.status(workspace, run_id)
        if state.get('status') == 'ALL_DONE':
            report = _outputs().generate_html(workspace, run_id, 'final',
                                               str(_run_root(run_id) / 'reports'))
            _write_control(run_id, status='all_done', message='', report_path=report,
                           report_mode='final')
            return
        if state.get('status') == 'PARTIAL_DONE':
            report = _outputs().generate_html(workspace, run_id, 'partial',
                                               str(_run_root(run_id) / 'reports'))
            _write_control(run_id, status='partial_done', message='运行已部分完成',
                           report_path=report, report_mode='partial')
            return
        if client is None:
            endpoint, key = settings.connection()
            client = _CLIENT_FACTORY(endpoint, key)
        with _LOCK:
            if _STOP.is_set() or _read_control(run_id).get('status') != 'running':
                return
            _BUSY_RUNS.add(run_id)
        try:
            batch = engine.lease_jobs(workspace, run_id, limit=1)
            if batch.get('busy'):
                # This is a previous process's lease. Do not force-take over automatically.
                _write_control(run_id, status='interrupted', message='存在未结束批次；请显式恢复')
                return
            if _read_control(run_id).get('status') in ('paused', 'pausing'):
                if batch.get('jobs'):
                    _release_unstarted_batch(run_id, batch)
                _write_control(run_id, status='paused', message='已暂停；未发送的任务已安全退回')
                return
            if not batch.get('jobs'):
                time.sleep(1.0)
                continue
            _process_batch(run_id, batch, client)
            control = _read_control(run_id)
            if _STOP.is_set() or control.get('status') in ('paused', 'pausing'):
                return
        finally:
            _finish_worker_boundary(run_id)


def _worker_loop() -> None:
    global _ACTIVE_RUN
    while not _STOP.is_set():
        with _LOCK:
            run_id = _QUEUE.pop(0) if _QUEUE else None
            _ACTIVE_RUN = run_id
            _WAKE.clear()
        if not run_id:
            _WAKE.wait(0.5)
            continue
        try:
            _run_one(run_id)
        except Exception as exc:  # noqa: BLE001
            _write_control(run_id, status='interrupted', message=scrub(exc))
        finally:
            with _LOCK:
                _ACTIVE_RUN = None
                reschedule = run_id in _RESCHEDULE
                _RESCHEDULE.discard(run_id)
                if reschedule and _read_control(run_id).get('status') == 'running' and run_id not in _QUEUE:
                    _QUEUE.append(run_id)
