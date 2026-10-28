"""One serial, in-process Sorftime worker over the vendored xlj_core state machine."""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
import math
import time
import uuid
from datetime import datetime, timezone
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
    with _LOCK:
        path = _control_path(run_id)
        state = _read_control(run_id)
        state.update(fields)
        state['updated_at'] = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
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


def list_presets() -> list[dict]:
    path = root() / 'presets.json'
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise ValueError('策略预设文件无法读取') from None
    if not isinstance(values, list):
        raise ValueError('策略预设数据结构无效')
    return values


def _save_presets(values: list[dict]) -> None:
    path = root() / 'presets.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def create_preset(name: str, policy: dict) -> dict:
    name = str(name or '').strip()
    if not name or len(name) > 100:
        raise ValueError('预设名称不能为空且不得超过 100 字')
    chosen = _engine()._validated_policy(policy or {})
    presets = list_presets()
    if any(str(item.get('name', '')).casefold() == name.casefold() for item in presets):
        raise ValueError('预设名称已存在')
    preset = {'preset_id': str(uuid.uuid4()), 'name': name, 'policy': chosen,
              'created_at': _now(), 'updated_at': _now()}
    presets.append(preset)
    _save_presets(presets)
    return preset


def update_preset(preset_id: str, name: str | None = None,
                  policy: dict | None = None) -> dict:
    presets = list_presets()
    preset = next((item for item in presets if item.get('preset_id') == preset_id), None)
    if preset is None:
        raise ValueError('策略预设不存在')
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError('预设名称不能为空且不得超过 100 字')
        if any(item is not preset and str(item.get('name', '')).casefold() == name.casefold() for item in presets):
            raise ValueError('预设名称已存在')
        preset['name'] = name
    if policy is not None:
        preset['policy'] = _engine()._validated_policy(policy)
    preset['updated_at'] = _now()
    _save_presets(presets)
    return preset


def delete_preset(preset_id: str) -> dict:
    presets = list_presets()
    kept = [item for item in presets if item.get('preset_id') != preset_id]
    if len(kept) == len(presets):
        raise ValueError('策略预设不存在')
    _save_presets(kept)
    return {'preset_id': preset_id, 'deleted': True}


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
               max_nodes: int | None = None, *, category: str | None = None,
               operator: str | None = None, policy: dict | None = None) -> dict:
    settings.connection()
    engine = _engine()
    site = engine.normalize_site(site)
    node_id = engine._validate_node_id(node_id)
    chosen = dict(policy or {})
    if max_depth is not None and 'max_level' not in chosen:
        chosen['max_level'] = max_depth
    chosen = engine._validated_policy(chosen)
    depth = chosen['max_level']
    node_cap = None if max_nodes is None else int(max_nodes)
    if node_cap is not None and not 1 <= node_cap <= 100000:
        raise ValueError('max_nodes 必须在 1 至 100000 之间')
    category_name = str(node_id if category is None else category).strip()
    operator_name = str(operator or '').strip()
    if not category_name:
        raise ValueError('category 不能为空')
    run_id = str(uuid.uuid4())
    run_dir = root() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / 'workspace'
    workspace.mkdir()
    report_dir = run_dir / 'reports'
    report_dir.mkdir()
    if node_cap is not None:
        chosen['max_nodes'] = node_cap
    engine.create_run(str(workspace), site, str(node_id), category_name,
                      policy=chosen, operator_name=operator_name,
                      report_dir=str(report_dir), run_id=run_id)
    _write_control(run_id, status='running', site=site.upper(), node_id=str(node_id),
                   category=category_name, operator=operator_name,
                   policy=chosen, max_depth=depth, max_nodes=node_cap, message='', report_path='',
                   revision=1, name='', note='', archived=False, trashed=False,
                   archived_at=None, trashed_at=None, needs_rerun=False)
    start()
    _schedule(run_id)
    return {'run_id': run_id, 'id': run_id, 'status': 'running', 'reused': False,
            'revision': 1, 'metadata': _metadata(_read_control(run_id))}


def _engine_state(run_id: str) -> dict:
    return _engine().status(str(_workspace(run_id)), run_id)


def list_runs(search: str = '', status: str = '', sort: str = 'updated_at',
              direction: str = 'desc', archived: bool | None = None,
              trashed: bool | None = None) -> list[dict]:
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
    query = str(search or '').strip().casefold()
    if query:
        rows = [row for row in rows if query in ' '.join(str(row.get(key) or '') for key in
                 ('id', 'node_id', 'category', 'operator', 'name', 'note', 'site')).casefold()]
    if status:
        rows = [row for row in rows if str(row.get('status', '')).casefold() == str(status).casefold()]
    if archived is not None:
        rows = [row for row in rows if bool(row.get('archived')) is archived]
    if trashed is not None:
        rows = [row for row in rows if bool(row.get('trashed')) is trashed]
    allowed_sort = {'updated_at', 'created_at', 'category', 'operator', 'name', 'status', 'site'}
    if sort not in allowed_sort:
        raise ValueError('sort 不受支持')
    if direction.lower() not in ('asc', 'desc'):
        raise ValueError('direction 必须是 asc 或 desc')
    return sorted(rows, key=lambda item: str(item.get(sort) or '').casefold(),
                  reverse=direction.lower() == 'desc')


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
    state['category'] = control.get('category') or state.get('category') or control.get('node_id')
    state['operator'] = control.get('operator') or state.get('operator_name') or ''
    state['revision'] = int(control.get('revision') or 1)
    state['metadata'] = _metadata(control)
    state.update(state['metadata'])
    state['incomplete'] = bool(control.get('node_cap_reached'))
    if control.get('incomplete_reason'):
        state['incomplete_reason'] = control['incomplete_reason']
    if (not control.get('report_path') and engine_status in ('ALL_DONE', 'PARTIAL_DONE')):
        state['needs_rerun'] = True
        state['report_needs_rerun'] = True
    else:
        state['needs_rerun'] = bool(control.get('needs_rerun'))
        state['report_needs_rerun'] = bool(control.get('needs_rerun'))
    if control.get('report_path') and engine_status in ('ALL_DONE', 'PARTIAL_DONE'):
        try:
            reports_dir = (_run_root(run_id) / 'reports').resolve()
            report_path = Path(control['report_path']).resolve()
            if report_path.parent != reports_dir or not report_path.is_file() or report_path.stat().st_size == 0:
                state['needs_rerun'] = state['report_needs_rerun'] = True
        except (OSError, ValueError):
            state['needs_rerun'] = state['report_needs_rerun'] = True
    if (engine_status in ('ALL_DONE', 'PARTIAL_DONE') and not state['needs_rerun']
            and not _has_valid_report_data(run_id, control)):
        state['needs_rerun'] = state['report_needs_rerun'] = True
        state['needs_rerun_reason'] = '历史报告没有有效的量价判断或明确的空类目结果；请用修复后的版本重新扫描。'
    report_ready = ((engine_status == 'ALL_DONE' and control.get('report_mode') == 'final')
                    or (engine_status == 'PARTIAL_DONE' and control.get('report_mode') == 'partial'))
    state['report_url'] = (f'/api/xunlongjue/runs/{run_id}/report'
                           if report_ready and control.get('report_path') and not state['needs_rerun'] else None)
    state['updated_at'] = control.get('updated_at') or state.get('updated_at')
    retry_info = _retry_info(run_id, state.get('policy') or {})
    if retry_info:
        state['retry_info'] = retry_info
    return state


def _has_valid_report_data(run_id: str, control: dict) -> bool:
    """Reject old terminal reports that contain neither decisions nor a verified empty root."""
    db = importlib.import_module('.vendor.xlj_core.db', package=__package__)
    conn = db.connect(str(_workspace(run_id)))
    try:
        root_id = str(control.get('node_id') or '')
        root = conn.execute(
            "SELECT result FROM nodes WHERE run_id=? AND node_id=?", (run_id, root_id),
        ).fetchone()
        decisions = int(conn.execute(
            'SELECT COUNT(*) FROM decisions WHERE run_id=?', (run_id,),
        ).fetchone()[0])
        if decisions:
            return True
        if not root or root['result'] != 'ROOT_EMPTY':
            return False
        successful_root_tree = conn.execute(
            "SELECT 1 FROM jobs WHERE run_id=? AND node_id=? AND job_type='category_tree' "
            "AND state='SUCCEEDED' LIMIT 1", (run_id, root_id),
        ).fetchone()
        return successful_root_tree is not None
    finally:
        conn.close()


def _metadata(control: dict) -> dict:
    return {key: control.get(key) for key in
            ('revision', 'name', 'note', 'archived', 'trashed', 'archived_at', 'trashed_at')}


def update_metadata(run_id: str, revision: int, name: str | None = None,
                    note: str | None = None) -> dict:
    try:
        revision = int(revision)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('revision 必须是整数') from None
    with _LOCK:
        control = _read_control(run_id)
        if not control:
            raise ValueError('运行不存在')
        if revision != int(control.get('revision') or 1):
            raise RevisionConflict('运行信息已被其他编辑更新，请刷新后重试')
        fields = {}
        if name is not None:
            fields['name'] = str(name).strip()[:200]
        if note is not None:
            fields['note'] = str(note).strip()[:5000]
        fields['revision'] = int(control.get('revision') or 1) + 1
        _write_control(run_id, **fields)
    return get_run(run_id)


class RevisionConflict(ValueError):
    """Optimistic metadata revision conflict; routes map it to HTTP 409."""


def _is_active(state: dict) -> bool:
    return str(state.get('status') or '').lower() in {'running', 'queued', 'pausing', 'resuming'}


def bulk_runs(action: str, run_ids: list[str]) -> dict:
    if action not in {'archive', 'trash', 'restore'}:
        raise ValueError('action 必须是 archive、trash 或 restore')
    ids = list(dict.fromkeys(_validate_run_id(value) for value in run_ids or []))
    if not ids:
        raise ValueError('run_ids 不能为空')
    states = [(run_id, get_run(run_id)) for run_id in ids]
    active = [run_id for run_id, state in states if _is_active(state)]
    if active:
        raise ValueError('活动运行不能归档、移入回收站或恢复')
    stamp = _now()
    for run_id, _ in states:
        if action == 'archive':
            _write_control(run_id, archived=True, archived_at=stamp)
        elif action == 'trash':
            _write_control(run_id, archived=False, archived_at=None,
                           trashed=True, trashed_at=stamp)
        else:
            _write_control(run_id, archived=False, trashed=False,
                           archived_at=None, trashed_at=None)
    return {'updated': ids, 'runs': [get_run(run_id) for run_id in ids]}


def _retry_info(run_id: str, policy: dict) -> dict | None:
    """Summarize retryable/failed jobs without exposing raw transport diagnostics."""
    workspace = str(_workspace(run_id))
    db = importlib.import_module('.vendor.xlj_core.db', package=__package__)
    conn = db.connect(workspace)
    try:
        retry_rows = conn.execute(
            "SELECT node_id,attempts,state,eligible_at,last_error,updated_at "
            "FROM jobs WHERE run_id=? AND attempts>0 AND state IN ('PENDING','FAILED') "
            "ORDER BY updated_at DESC,job_id",
            (run_id,),
        ).fetchall()
        if not retry_rows:
            return None
        pending = [row for row in retry_rows if row['state'] == 'PENDING']
        last_error_row = next((row for row in retry_rows if row['last_error']), retry_rows[0])
        _, api_key = settings.connection()
        suspended_count = int(conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE run_id=? AND state='SUSPENDED'", (run_id,),
        ).fetchone()[0])
        summary = {
            'retry_count': len(retry_rows),
            'waiting_count': len(pending),
            'exhausted_count': sum(row['state'] == 'FAILED' for row in retry_rows),
            'suspended_count': suspended_count,
            'next_retry_at': min((row['eligible_at'] for row in pending if row['eligible_at']), default=None),
            'last_error': scrub(last_error_row['last_error'], api_key),
            'last_node_id': str(last_error_row['node_id']),
            'last_attempt': int(last_error_row['attempts']),
            'max_attempts': int(policy.get('max_attempts') or 0),
        }
        return summary
    except ValueError:
        # Settings may be absent after credential removal. Stored errors are already
        # scrubbed on ingest; keep diagnostics available without requiring a key.
        return {
            'retry_count': len(retry_rows),
            'waiting_count': sum(row['state'] == 'PENDING' for row in retry_rows),
            'exhausted_count': sum(row['state'] == 'FAILED' for row in retry_rows),
            'suspended_count': int(conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE run_id=? AND state='SUSPENDED'", (run_id,),
            ).fetchone()[0]),
            'next_retry_at': min((row['eligible_at'] for row in retry_rows
                                      if row['state'] == 'PENDING' and row['eligible_at']), default=None),
            'last_error': scrub(last_error_row['last_error']),
            'last_node_id': str(last_error_row['node_id']),
            'last_attempt': int(last_error_row['attempts']),
            'max_attempts': int(policy.get('max_attempts') or 0),
        }
    finally:
        conn.close()


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


def _tree_item_count(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ('items', 'subcategories', 'data'):
            if isinstance(payload.get(key), list):
                return len(payload[key])
    return 0


def _cap_tree_result(run_id: str, payload, reserved_nodes: int = 0):
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
    remaining = max(0, int(cap) - int(state.get('total_nodes') or 0) - int(reserved_nodes))
    if len(raw) > remaining:
        raise NodeCapReached('达到 max_nodes 节点上限；Sorftime 类目树未截断，本分支未判定为叶节点或机会')
    return payload


class NodeCapReached(ValueError):
    """The node cap stopped expansion without fabricating a leaf or opportunity."""


def _tree_no_data_context(run_id: str, job: dict) -> dict:
    """Prove that an empty-tree sentinel can only close an eligible child job."""
    node_id = str(job.get('node_id') or '')
    job_id = str(job.get('job_id') or '')
    try:
        db_module = importlib.import_module('.vendor.xlj_core.db', package=__package__)
        conn = db_module.connect(str(_workspace(run_id)))
        try:
            run = conn.execute(
                'SELECT root_node_id FROM runs WHERE run_id=?', (run_id,),
            ).fetchone()
            node = conn.execute(
                'SELECT 1 FROM nodes WHERE run_id=? AND node_id=?', (run_id, node_id),
            ).fetchone()
            parent = conn.execute(
                'SELECT 1 FROM edges WHERE run_id=? AND child_id=? LIMIT 1',
                (run_id, node_id),
            ).fetchone()
            tree_job = conn.execute(
                "SELECT 1 FROM jobs WHERE run_id=? AND job_id=? AND node_id=? "
                "AND job_type='category_tree' AND state='LEASED'",
                (run_id, job_id, node_id),
            ).fetchone()
            trends = conn.execute(
                "SELECT indicator,state FROM jobs WHERE run_id=? AND node_id=? "
                "AND job_type='category_trend'",
                (run_id, node_id),
            ).fetchall()
            successful_indicators = {
                str(row['indicator']) for row in trends if row['state'] == 'SUCCEEDED'
            }
            return {
                # Unknown run metadata must never be treated as proof of a
                # non-root node; the Sorftime no-data fallback fails closed.
                'is_root': None if run is None else str(run['root_node_id']) == node_id,
                'node_known': bool(node),
                'parent_edge_found': bool(parent),
                'tree_job_leased': bool(tree_job),
                'successful_trend_count': len(successful_indicators.intersection(
                    {'SalesCount', 'AvgPrice'},
                )),
            }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - fail closed, preserve useful type only.
        return {
            'is_root': None,
            'node_known': False,
            'parent_edge_found': False,
            'tree_job_leased': False,
            'successful_trend_count': 0,
            'context_read_error_type': type(exc).__name__,
        }


def _call_for_job(client, job: dict, run_id: str = '') -> dict:
    kind = job.get('type')
    if kind not in ('category_tree', 'category_trend'):
        raise ValueError('引擎返回未知任务类型')
    # Map internal lease fields to the documented Sorftime MCP wire names.
    arguments = {'amz_site': str(job.get('site') or ''),
                 'node_id': str(job.get('node_id') or '')}
    if kind == 'category_trend':
        arguments['trend_index'] = str(job.get('indicator') or '')
    audit_context = {
        'run_id': run_id,
        'job_id': str(job.get('job_id') or ''),
        'node_id': str(job.get('node_id') or ''),
        'site': str(job.get('site') or ''),
        'job_type': kind,
        'indicator': str(job.get('indicator') or ''),
        'attempt': int(job.get('attempt') or 0),
    }
    if kind == 'category_tree' and run_id:
        audit_context.update(_tree_no_data_context(run_id, job))
    usage_event_id = None
    try:
        from ..usage import start_usage_event
        usage_event_id = start_usage_event(
            kind="mcp", module="xunlongjue", task_id=run_id or None,
            function_name=kind, server_name="Sorftime", attempt=int(job.get("attempt") or 1),
            metadata={"job_id": str(job.get("job_id") or "")},
        )
    except Exception:
        usage_event_id = None
    usage_started = time.perf_counter()
    try:
        payload = call_sync(client, kind, arguments, audit_context=audit_context)
    except Exception:
        try:
            from ..usage import finish_usage_event
            finish_usage_event(usage_event_id, status="failed",
                               duration_ms=round((time.perf_counter()-usage_started)*1000))
        except Exception:
            pass
        raise
    try:
        from ..usage import finish_usage_event
        finish_usage_event(usage_event_id, status="success",
                           duration_ms=round((time.perf_counter()-usage_started)*1000))
    except Exception:
        pass
    if kind == 'category_trend':
        return _normalize_sorftime_trend(payload, str(job.get('indicator') or ''))
    return payload


def _normalize_sorftime_trend(payload: object, indicator: str) -> dict:
    """Convert Sorftime's data rows (`YYYY-MM=value`) to the core's stable shape."""
    if isinstance(payload, dict):
        rows = payload.get('data')
        if rows is None:
            months, values = payload.get('months'), payload.get('values')
            if not isinstance(months, list) or not isinstance(values, list) or len(months) != len(values):
                raise ValueError('Sorftime 趋势响应缺少 data 列表')
            pairs = list(zip(months, values))
        else:
            if not isinstance(rows, list):
                raise ValueError('Sorftime 趋势 data 必须是数组')
            pairs = []
            for row in rows:
                if isinstance(row, str):
                    month, sep, value = row.partition('=')
                    if not sep:
                        raise ValueError('Sorftime 趋势 data 行必须使用 YYYY-MM=value 格式')
                    pairs.append((month.strip(), value.strip()))
                elif isinstance(row, dict):
                    month = next((row.get(k) for k in ('month', 'Month', '月份', 'date') if row.get(k) is not None), None)
                    value = next((row.get(k) for k in (indicator, indicator.lower(), 'value', 'Value', 'count', 'price') if row.get(k) is not None), None)
                    if month is None or value is None:
                        raise ValueError('Sorftime 趋势 data 项缺少月份或数值')
                    pairs.append((month, value))
                else:
                    raise ValueError('Sorftime 趋势 data 项类型无效')
    elif isinstance(payload, list):
        pairs = []
        for row in payload:
            if isinstance(row, str):
                month, sep, value = row.partition('=')
                if not sep:
                    raise ValueError('Sorftime 趋势 data 行必须使用 YYYY-MM=value 格式')
                pairs.append((month.strip(), value.strip()))
            elif isinstance(row, dict):
                month = next((row.get(k) for k in ('month', 'Month', '月份', 'date') if row.get(k) is not None), None)
                value = next((row.get(k) for k in (indicator, indicator.lower(), 'value', 'Value', 'count', 'price') if row.get(k) is not None), None)
                if month is None or value is None:
                    raise ValueError('Sorftime 趋势 data 项缺少月份或数值')
                pairs.append((month, value))
            else:
                raise ValueError('Sorftime 趋势 data 项类型无效')
    else:
        raise ValueError('Sorftime 趋势响应结构无效')

    from .vendor.xlj_core.engine import _normalize_month
    now = datetime.now(timezone.utc)
    latest_complete = now.year * 12 + now.month - 2
    points = {}
    for month, raw_value in pairs:
        normalized, index = _normalize_month(month)
        if index > latest_complete:
            continue
        if index in points:
            raise ValueError('Sorftime 趋势月份重复')
        if isinstance(raw_value, bool):
            raise ValueError('Sorftime 趋势数值必须为有限数字')
        try:
            value = float(str(raw_value).replace(',', '').strip())
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Sorftime 趋势数值必须为有限数字') from None
        if not math.isfinite(value):
            raise ValueError('Sorftime 趋势数值必须为有限数字')
        points[index] = (normalized, value)
    if not points:
        raise ValueError('Sorftime 趋势没有完整有限数据点')
    end = max(points)
    indexes = sorted(points)
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise ValueError('Sorftime 趋势完整月份必须连续')
    start = max(indexes[0], end - 23)
    selected = [points[index] for index in range(start, end + 1)]
    if len(selected) < 6:
        raise ValueError(f'Sorftime 趋势连续完整数据不足 6 个月: {len(selected)}')
    return {'months': [point[0] for point in selected],
            'values': [point[1] for point in selected]}


def _process_batch(run_id: str, batch: dict, client) -> None:
    engine = _engine()
    results = []
    reserved_nodes = 0
    jobs = list(batch.get('jobs') or [])
    for index, job in enumerate(jobs):
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
            unstarted = jobs[index:]
            _release_unstarted_batch(run_id, batch, unstarted,
                                     preserve_completed=bool(results))
            if not results:
                _write_control(run_id, status='paused', message='已暂停；未发送的任务已安全退回')
                return
            batch['jobs'] = jobs[:index]
            break
        try:
            payload = _call_for_job(client, job, run_id)
            if job.get('type') == 'category_tree':
                payload = _cap_tree_result(run_id, payload, reserved_nodes)
                reserved_nodes += _tree_item_count(payload)
            result = {'job_id': job['job_id'], 'attempt': job['attempt'],
                      'lease_token': job['lease_token'], 'status': 'success', 'payload': payload}
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, NodeCapReached):
                _write_control(run_id, node_cap_reached=True,
                               incomplete_reason=str(exc), needs_rerun=False)
            result = {'job_id': job['job_id'], 'attempt': job['attempt'],
                      'lease_token': job['lease_token'], 'status': 'failed',
                      'error': scrub(exc, client.api_key)}
        finally:
            with _LOCK:
                _CALLING_RUNS.discard(run_id)
        results.append(result)
        engine.pulse_batch(str(_workspace(run_id)), run_id, batch['batch_id'],
                           completed=sum(x['status'] == 'success' for x in results),
                           failed=sum(x['status'] == 'failed' for x in results),
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


def _release_unstarted_batch(run_id: str, batch: dict, jobs: list[dict] | None = None,
                             *, preserve_completed: bool = False) -> None:
    """Return unstarted jobs without consuming attempts, retaining completed lease results."""
    workspace = str(_workspace(run_id))
    xlj_db = importlib.import_module('.vendor.xlj_core.db', package=__package__)
    conn = xlj_db.connect(workspace)
    try:
        with xlj_db.transaction(conn):
            unstarted = list(jobs if jobs is not None else batch.get('jobs') or [])
            for job in unstarted:
                conn.execute(
                    "UPDATE jobs SET state='PENDING',attempts=MAX(0,attempts-1),"
                    "lease_until=NULL,lease_token='',eligible_at=?,updated_at=? "
                    "WHERE job_id=? AND state='LEASED' AND attempts=? AND lease_token=?",
                    (xlj_db.utc_now(), xlj_db.utc_now(), job.get('job_id'),
                     job.get('attempt'), job.get('lease_token')),
                )
                conn.execute('DELETE FROM batch_jobs WHERE batch_id=? AND job_id=?',
                             (batch['batch_id'], job.get('job_id')))
            remaining = int(conn.execute(
                'SELECT COUNT(*) FROM batch_jobs WHERE batch_id=?', (batch['batch_id'],),
            ).fetchone()[0])
            if remaining:
                conn.execute('UPDATE batches SET job_count=?,updated_at=? WHERE batch_id=?',
                             (remaining, xlj_db.utc_now(), batch['batch_id']))
            elif not preserve_completed:
                conn.execute("UPDATE batches SET state='ABANDONED',job_count=0,lease_until=NULL,updated_at=? WHERE batch_id=?",
                             (xlj_db.utc_now(), batch['batch_id']))
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
            batch_size = int((state.get('policy') or {}).get('batch_size') or 20)
            batch = engine.lease_jobs(workspace, run_id, limit=batch_size)
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
