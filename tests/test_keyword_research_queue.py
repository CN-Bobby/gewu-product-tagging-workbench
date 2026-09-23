"""队列/快照接口的隔离存储回归；不启动真实 MCP 或模型调用。"""
import ctypes
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.keyword_research import credentials, registry, runner


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    root = tmp_path / 'keyword-research'
    monkeypatch.setattr(registry, 'ROOT', root)
    root.mkdir()
    monkeypatch.setenv('TAG_PROVIDER_HOME', str(tmp_path / 'provider-home'))
    return root


def _create(display_name='task'):
    return registry.create({'groups': [], 'options': {}},
                           {'confirmed': True, 'budget': {'planned_total': 4},
                            'selection': {'model_ref': 'provider/model', 'credential_id': 'cred-1'}},
                           display_name=display_name)


def test_created_runs_are_not_dispatched_and_order_is_persisted(isolated_runs):
    ids = [_create(f'task-{i}') for i in range(3)]
    assert all(registry.read(run_id)['status'] == registry.CREATED for run_id in ids)
    queue = registry.enqueue(ids, enable_dispatch=False)
    assert queue['items'] == ids and queue['dispatch_enabled'] is False
    reordered = [ids[2], ids[0], ids[1]]
    registry.reorder(reordered)
    assert json.loads(registry.queue_path().read_text(encoding='utf-8'))['items'] == reordered
    assert registry.read(ids[0])['attempt'] == 0
    assert registry.read(ids[0])['status'] == registry.QUEUED


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_scheduler_runs_three_in_requested_order_and_advances_after_failure(
        isolated_runs, monkeypatch):
    created = [_create(f'task-{index}') for index in range(3)]
    requested = [created[2], created[0], created[1]]
    order = []
    completed = threading.Event()
    def execute(run_id, cancel_event=None):
        state = registry.read(run_id)
        attempt = int(state.get('attempt') or 0) + 1
        registry.write(run_id, status=registry.RUNNING, attempt=attempt)
        order.append(run_id)
        if run_id == requested[0]:
            registry.write(run_id, status=registry.FAILED, error='synthetic failure')
            registry.append_event(run_id, 'run_failed', {'error': 'synthetic failure'})
            raise RuntimeError('synthetic failure')
        registry.write(run_id, status=registry.DONE)
        registry.append_event(run_id, 'run_finished', {})
        if len(order) == 3:
            completed.set()
    monkeypatch.setattr(runner, 'execute', execute)

    runner.start_queue(requested)

    assert completed.wait(3)
    assert _wait_for(lambda: registry.queue_state()['current'] is None)
    assert order == requested
    assert [registry.read(run_id)['status'] for run_id in requested] == [
        registry.FAILED, registry.DONE, registry.DONE]
    assert all(registry.read(run_id)['attempt'] == 1 for run_id in requested)


def test_single_task_cancel_advances_to_next_task(isolated_runs, monkeypatch):
    first, second = _create('cancel me'), _create('next')
    started = threading.Event()
    order = []
    second_done = threading.Event()
    def execute(run_id, cancel_event=None):
        state = registry.read(run_id)
        registry.write(run_id, status=registry.RUNNING, attempt=int(state.get('attempt') or 0) + 1)
        order.append(run_id)
        if run_id == first:
            started.set()
            cancel_event.wait(3)
            registry.write(run_id, status=registry.CANCELLED, message='已取消')
            registry.append_event(run_id, 'run_cancelled', {})
            return {'status': registry.CANCELLED}
        registry.write(run_id, status=registry.DONE)
        registry.append_event(run_id, 'run_finished', {})
        second_done.set()
        return {'status': registry.DONE}
    monkeypatch.setattr(runner, 'execute', execute)

    runner.start_queue([first, second])
    assert started.wait(2)
    assert runner.cancel(first) == registry.CANCELLING
    assert second_done.wait(3)
    assert _wait_for(lambda: registry.queue_state()['current'] is None)
    assert order == [first, second]
    assert registry.read(first)['status'] == registry.CANCELLED
    assert registry.read(second)['status'] == registry.DONE


def test_restart_interruption_requires_explicit_continue(isolated_runs):
    running, waiting = _create('running'), _create('waiting')
    registry.enqueue([running, waiting], enable_dispatch=True)
    queue = registry.queue_state()
    queue['current'] = running
    registry._save_queue(queue)
    registry.write(running, status=registry.RUNNING)

    registry.recover()

    assert registry.read(running)['status'] == registry.INTERRUPTED
    assert registry.read(waiting)['status'] == registry.QUEUED
    assert registry.queue_state()['items'] == [running, waiting]
    assert registry.queue_state()['dispatch_enabled'] is False
    registry.continue_queue()
    assert registry.read(running)['status'] == registry.QUEUED
    assert registry.queue_state()['dispatch_enabled'] is True


def test_recover_clears_claimed_but_not_started_current_item(isolated_runs):
    claimed, waiting = _create('claimed'), _create('waiting')
    registry.enqueue([claimed, waiting], enable_dispatch=True)
    queue = registry.queue_state(); queue['current'] = claimed; registry._save_queue(queue)
    # Crash between durable claim_next() and worker's first RUNNING snapshot.
    assert registry.read(claimed)['status'] == registry.QUEUED
    registry.recover()
    assert registry.queue_state()['current'] is None
    assert registry.queue_state()['items'] == [claimed, waiting]
    assert registry.queue_state()['dispatch_enabled'] is False
    registry.continue_queue()
    assert registry.queue_state()['dispatch_enabled'] is True


def test_stop_queue_skips_waiting_items_and_keeps_current_for_cancel(isolated_runs):
    current, waiting = _create('current'), _create('waiting')
    registry.enqueue([current, waiting], enable_dispatch=True)
    queue = registry.queue_state(); queue['current'] = current; registry._save_queue(queue)
    registry.write(current, status=registry.RUNNING)

    stopped = registry.stop_queue()

    assert stopped['dispatch_enabled'] is False
    assert stopped['current'] == current
    assert registry.read(waiting)['status'] == registry.SKIPPED
    assert registry.read_events(waiting)[-1]['event_type'] == 'run_skipped'


def test_retry_uses_same_run_directory_and_preserves_cache(isolated_runs):
    run_id = _create()
    cache = registry.run_dir(run_id) / 'cache' / 'checkpoint.json'
    cache.write_text('{"cached":true}', encoding='utf-8')
    registry.write(run_id, status=registry.FAILED, error='synthetic')
    registry.enqueue([run_id])
    assert registry.run_dir(run_id) == cache.parent.parent
    assert json.loads(cache.read_text(encoding='utf-8')) == {'cached': True}
    assert registry.read(run_id)['attempt'] == 0
    registry.write(run_id, status=registry.FAILED)
    registry.enqueue([run_id])
    assert registry.read(run_id)['attempt'] == 0


def test_pagination_has_no_silent_200_item_boundary(isolated_runs):
    ids = [_create() for _ in range(203)]
    first, cursor = registry.list_runs(limit=200)
    second, next_cursor = registry.list_runs(limit=200, cursor=cursor)
    assert len(first) == 200 and len(second) == 3
    assert next_cursor is None
    assert len({x['run_id'] for x in first + second}) == len(ids)


def test_model_selection_freezes_credential_id_without_serializing_secret(tmp_path, monkeypatch):
    from backend.model_settings import _write_config
    from backend.runtime_isolation import CredentialUnavailable

    home = tmp_path / 'provider-home'; home.mkdir()
    _write_config(home / 'config.toml', {'default_model': 'fake/m1', 'providers': [{
        'name': 'fake', 'kind': 'openai', 'base_url': 'https://fake.invalid',
        'models': ['m1'], 'credentials': [
            {'id': 'first', 'name': 'First', 'api_key_env': 'KEY_A', 'enabled': True},
            {'id': 'disabled', 'name': 'Disabled', 'api_key_env': 'KEY_B', 'enabled': False}],
        'primary_credential_id': 'first'}]})
    (home / '.env').write_text('KEY_A=synthetic-key\nKEY_B=disabled-key\n', encoding='utf-8')
    monkeypatch.setenv('TAG_PROVIDER_HOME', str(home))

    selected = credentials.model_selection('fake/m1', 'first')
    assert selected == {'model_ref': 'fake/m1', 'credential_id': 'first'}
    with pytest.raises(CredentialUnavailable, match='停用'):
        credentials.model_selection('fake/m1', 'disabled')
    assert 'synthetic-key' not in json.dumps(selected)


def test_create_only_route_and_legacy_default_enqueue(monkeypatch, isolated_runs):
    monkeypatch.setattr(credentials, 'model_selection',
                        lambda model_ref=None, credential_id=None: {
                            'model_ref': model_ref or 'fake/m1', 'credential_id': credential_id or 'key-1'})
    enqueued = []
    monkeypatch.setattr(runner, 'start_queue', lambda ids: enqueued.extend(ids))
    with TestClient(app_module.app) as client:
        synthetic_asin = 'B0' + 'SYN00001'
        request = {'groups': [{'marketplace': 'US', 'asins': [synthetic_asin]}],
                   'plan_hash': None}
        planned = client.post('/api/keyword-research/plan', json=request).json()
        changed_selection = client.post('/api/keyword-research/plan', json={
            **request, 'model_ref': 'fake/other', 'credential_id': 'other-key'}).json()
        assert planned['plan_hash'] != changed_selection['plan_hash']
        assert planned['credential_id'] == 'key-1'
        body = {'groups': request['groups'], 'plan_hash': planned['plan_hash'], 'display_name': 'only create',
                'model_ref': planned['model_ref'], 'credential_id': planned['credential_id'],
                'dispatch': 'create'}
        created = client.post('/api/keyword-research/runs', json=body)
        assert created.status_code == 200
        run_id = created.json()['run_id']
        assert registry.read(run_id)['status'] == registry.CREATED
        assert enqueued == []

        legacy_body = {**body, 'display_name': 'old client'}
        legacy_body.pop('dispatch')
        legacy = client.post('/api/keyword-research/runs', json=legacy_body)
        assert legacy.status_code == 200
        assert legacy.json()['run_id'] in enqueued


def test_management_routes_expose_queue_snapshot_and_task_actions(monkeypatch, isolated_runs):
    ids = [_create('first'), _create('second')]
    monkeypatch.setattr(runner, 'start_queue', lambda run_ids: registry.enqueue(run_ids))
    with TestClient(app_module.app) as client:
        queued = client.post('/api/keyword-research/queue', json={'run_ids': ids})
        assert queued.status_code == 200
        assert [item['run_id'] for item in queued.json()['items']] == ids
        assert queued.json()['items'][0]['budget']['planned_total'] == 4
        reordered = client.put('/api/keyword-research/queue/order',
                               json={'run_ids': list(reversed(ids))})
        assert [item['run_id'] for item in reordered.json()['items'][:2]] == list(reversed(ids))
        renamed = client.patch(f'/api/keyword-research/runs/{ids[0]}',
                               json={'display_name': 'renamed'})
        assert renamed.status_code == 200 and renamed.json()['display_name'] == 'renamed'
        assert client.post(f'/api/keyword-research/runs/{ids[0]}/hide').status_code == 200
        assert ids[0] not in [item['run_id'] for item in client.get('/api/keyword-research/runs').json()['runs']]
        hidden = client.get('/api/keyword-research/runs?hidden=true').json()['runs']
        assert ids[0] in [item['run_id'] for item in hidden]
        assert client.post(f'/api/keyword-research/runs/{ids[0]}/unhide').status_code == 200
        registry.append_event(ids[0], 'log', {'line': 'cached progress line'})
        detail = client.get(f'/api/keyword-research/runs/{ids[0]}').json()
        assert detail['logs'] == [{'sequence': 2, 'line': 'cached progress line'}]
        assert detail['latest_sequence'] == 2  # run_queued + log
        assert detail['recent_activity'][-1]['sequence'] == detail['latest_sequence']


def test_trash_uses_absolute_double_nul_windows_shell_and_never_deletes_fallback(
        isolated_runs, monkeypatch):
    run_id = _create()
    calls = []
    from ctypes import wintypes
    class ShellFileOp(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', wintypes.LPVOID), ('lpszProgressTitle', wintypes.LPCWSTR)]
    def success(pointer):
        operation = ctypes.cast(pointer, ctypes.POINTER(ShellFileOp)).contents
        address = ctypes.cast(pointer, ctypes.c_void_p).value + ShellFileOp.pFrom.offset
        from_address = ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p)).contents.value
        path_length = len(str(registry.run_dir(run_id).resolve()))
        source = ctypes.cast(from_address, ctypes.POINTER(ctypes.c_wchar * (path_length + 2))).contents
        calls.append((operation.wFunc, operation.fFlags, ''.join(source)))
        return 0
    monkeypatch.setattr(registry.os, 'name', 'nt')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(shell32=SimpleNamespace(SHFileOperationW=success)),
                        raising=False)
    result = registry.move_to_recycle_bin(run_id)
    assert result == {'run_id': run_id, 'recycled': True}
    assert calls[0][0] == 3  # FO_DELETE
    assert calls[0][1] & 0x0040  # FOF_ALLOWUNDO
    assert calls[0][2] == str(registry.run_dir(run_id).resolve()) + '\0\0'
    assert registry.run_dir(run_id).is_dir()  # mock succeeded; no local permanent-delete fallback ran


def test_trash_shell_failure_does_not_delete_permanently(isolated_runs, monkeypatch):
    run_id = _create()
    from ctypes import wintypes
    class ShellFileOp(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', wintypes.LPVOID), ('lpszProgressTitle', wintypes.LPCWSTR)]
    def failure(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ShellFileOp)).contents.fAnyOperationsAborted = True
        return 0
    monkeypatch.setattr(registry.os, 'name', 'nt')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(shell32=SimpleNamespace(SHFileOperationW=failure)),
                        raising=False)
    with pytest.raises(OSError, match='回收站失败'):
        registry.move_to_recycle_bin(run_id)
    assert registry.run_dir(run_id).is_dir()


def test_running_task_cannot_be_trashed(isolated_runs):
    run_id = _create()
    registry.write(run_id, status=registry.RUNNING)
    with pytest.raises(ValueError, match='不可移入回收站'):
        registry.move_to_recycle_bin(run_id)


def test_corrupt_queue_json_fails_closed_with_actionable_error(isolated_runs):
    registry.queue_path().write_text('{broken', encoding='utf-8')
    with pytest.raises(registry.QueueStateCorrupt, match='停止派发'):
        registry.queue_state()


def test_shutdown_marks_current_interrupted_and_preserves_it_at_queue_head(isolated_runs):
    current, waiting = _create('current'), _create('waiting')
    registry.enqueue([current, waiting], enable_dispatch=True)
    state = registry.queue_state(); state['current'] = current; registry._save_queue(state)
    registry.write(current, status=registry.RUNNING)
    event = threading.Event()
    runner.RUNNING[current] = (event, None)
    try:
        runner.stop_all()
        assert event.is_set()
        assert registry.read(current)['status'] == registry.INTERRUPTED
        assert registry.queue_state()['items'] == [current, waiting]
        assert registry.queue_state()['current'] is None
        assert registry.queue_state()['dispatch_enabled'] is False
    finally:
        runner.RUNNING.pop(current, None)
        runner.SHUTTING_DOWN.discard(current)


def test_shutdown_with_live_stub_pipeline_emits_one_interrupted_terminal(
        isolated_runs, monkeypatch):
    run_id = _create('shutdown live')
    registry.enqueue([run_id], enable_dispatch=True)
    assert registry.claim_next() == run_id
    started = threading.Event()
    monkeypatch.setattr(runner.credentials, 'model_target',
                        lambda *args: ('https://fake.invalid', 'synthetic-key', 'm1'))
    monkeypatch.setattr(runner.credentials, 'mcp_endpoint', lambda: 'https://fake.invalid/mcp')
    monkeypatch.setattr(runner.engine_runner, 'asset_digest', lambda: {'sha256': 'a' * 64, 'files': 1})
    monkeypatch.setattr(runner, 'EngineConfig', lambda **kw: SimpleNamespace(**kw))
    class WaitingPipeline:
        def __init__(self, config, task_dir, store, on_event=None, cancel_event=None):
            self.cancel_event = cancel_event
        def run(self):
            started.set()
            self.cancel_event.wait(3)
            return {'status': 'canceled'}
    monkeypatch.setattr(runner, 'Pipeline', WaitingPipeline)

    thread = runner.launch(run_id)
    assert started.wait(2)
    runner.stop_all()
    thread.join(3)

    assert not thread.is_alive()
    assert registry.read(run_id)['status'] == registry.INTERRUPTED
    assert registry.read(run_id)['attempt'] == 1
    terminals = [event for event in registry.read_events(run_id)
                 if event['event_type'] == 'run_failed']
    assert len(terminals) == 1
    assert registry.queue_state()['items'][0] == run_id
    assert registry.queue_state()['dispatch_enabled'] is False


def test_public_summary_never_returns_input_identifiers(isolated_runs):
    run_id = registry.create({'groups': [{'marketplace': 'US', 'asins': ['private-input'],
                                          'node_ids': ['private-node']}]},
                             {'budget': {'planned_total': 7}})
    summary = registry.public_summary(registry.read(run_id))
    assert summary['marketplaces'] == ['US']
    assert summary['asin_count'] == 1 and summary['node_count'] == 1
    assert summary['budget']['planned_total'] == 7
    assert 'private-input' not in json.dumps(summary)
    assert 'private-node' not in json.dumps(summary)


def test_concurrent_snapshot_reads_retry_transient_windows_sharing_errors(
        isolated_runs, monkeypatch):
    run_id = _create('concurrent reader')
    original_read_text = type(registry.run_dir(run_id) / 'run.json').read_text
    counter_lock = threading.Lock()
    failures = {'injected': 0}
    def flaky_read_text(path, *args, **kwargs):
        if path.name == 'run.json':
            with counter_lock:
                if not failures['injected']:
                    failures['injected'] += 1
                    raise PermissionError(13, 'synthetic Windows sharing violation', str(path))
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(type(registry.run_dir(run_id) / 'run.json'), 'read_text', flaky_read_text)
    start = threading.Barrier(3)
    finished = threading.Event()
    errors = []
    snapshots = []

    def writer():
        try:
            start.wait()
            for index in range(30):
                registry.write(run_id, message=f'update-{index}')
                time.sleep(0.001)
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)
        finally:
            finished.set()

    def reader():
        try:
            start.wait()
            while not finished.is_set():
                snapshot = registry.read(run_id)
                if snapshot:
                    snapshots.append(snapshot['message'])
                time.sleep(0.001)
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)

    writers = threading.Thread(target=writer)
    readers = threading.Thread(target=reader)
    writers.start(); readers.start(); start.wait()
    writers.join(5); readers.join(5)

    assert not writers.is_alive() and not readers.is_alive()
    assert failures['injected'] == 1
    assert errors == []
    assert snapshots
    assert registry.read(run_id)['message'] == 'update-29'
