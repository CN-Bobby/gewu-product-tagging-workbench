import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.xunlongjue import router, service, settings
from backend.xunlongjue.sorftime import SorftimeClient


class FakeSorftime:
    def __init__(self, endpoint, api_key):
        self.endpoint = endpoint
        self.api_key = api_key
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'category_tree':
            return {'data': []}
        if name == 'category_trend':
            return {'months': ['2026-01', '2026-02', '2026-03',
                               '2026-04', '2026-05', '2026-06'],
                    'values': [1, 1, 1, 1, 1, 1]}
        raise AssertionError(f'unexpected Sorftime call: {name}')


@pytest.fixture
def xlj_home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, '_root', lambda: tmp_path / 'xunlongjue')
    monkeypatch.setattr(service, 'root', lambda: tmp_path / 'xunlongjue')
    service.stop_all()
    yield tmp_path / 'xunlongjue'
    service.stop_all()


def test_settings_separate_endpoint_and_key_and_reject_query_secrets(xlj_home):
    assert settings.save('https://mcp.example.test/mcp', 'secret-key-value') == {
        'endpoint': 'https://mcp.example.test/mcp', 'key_configured': True,
    }
    persisted = json.loads((xlj_home / 'settings.json').read_text(encoding='utf-8'))
    assert persisted == {'endpoint': 'https://mcp.example.test/mcp', 'api_key': 'secret-key-value'}
    assert 'secret-key-value' not in str(settings.status())
    with pytest.raises(ValueError):
        settings.save('https://mcp.example.test/mcp?secret-key=embedded', 'other-key')


def test_run_ids_are_validated_before_building_workspace_paths(xlj_home):
    with pytest.raises(ValueError, match='运行编号非法'):
        service._workspace('../outside')


def test_category_trend_uses_sorftime_tool_contract():
    client = FakeSorftime('https://mcp.example.test/mcp', 'fake-key')
    # This is the shape returned by engine.lease_jobs. Keep the leased indicator
    # alongside the arguments so the Sorftime adapter cannot silently drop it.
    service._call_for_job(client, {
        'type': 'category_trend', 'node_id': '123', 'site': 'US',
        'indicator': 'SalesCount',
        'arguments': {'amz_site': 'US', 'node_id': '123', 'trend_index': 'SalesCount'},
    })
    assert client.calls == [('category_trend', {
        'amz_site': 'US', 'node_id': '123', 'trend_index': 'SalesCount',
    })]


def test_category_tree_uses_sorftime_tool_contract():
    client = FakeSorftime('https://mcp.example.test/mcp', 'fake-key')
    service._call_for_job(client, {
        'type': 'category_tree', 'node_id': '12345', 'site': 'US',
        'arguments': {'amz_site': 'US', 'node_id': '12345'},
    })
    assert client.calls == [('category_tree', {'amz_site': 'US', 'node_id': '12345'})]


@pytest.mark.parametrize('content', [[], [SimpleNamespace(text='')],
                                     [SimpleNamespace(text='upstream returned HTML')]])
def test_sorftime_rejects_empty_or_plain_text_mcp_content(content, monkeypatch):
    client = SorftimeClient('https://mcp.example.test/mcp', 'fake-key')
    result = SimpleNamespace(isError=False, content=content)

    async def fake_call_live(name, arguments):
        from backend.xunlongjue.sorftime import _decode
        return _decode(result)

    # Replace the live MCP boundary so this test only exercises fake content.
    monkeypatch.setattr(client, '_call_live', fake_call_live)
    with pytest.raises(RuntimeError):
        import asyncio
        asyncio.run(client.call('category_tree', {'amz_site': 'US', 'node_id': '123'}))


def test_sorftime_rejects_tree_objects_without_recognized_list_field(monkeypatch):
    client = SorftimeClient('https://mcp.example.test/mcp', 'fake-key')
    result = SimpleNamespace(isError=False, content=[SimpleNamespace(text='{"status":"ok"}')])

    async def fake_call_live(name, arguments):
        from backend.xunlongjue.sorftime import _decode
        return _decode(result)

    monkeypatch.setattr(client, '_call_live', fake_call_live)
    with pytest.raises(RuntimeError):
        import asyncio
        asyncio.run(client.call('category_tree', {'amz_site': 'US', 'node_id': '123'}))


def test_sorftime_accepts_a_genuine_empty_tree_list(monkeypatch):
    client = SorftimeClient('https://mcp.example.test/mcp', 'fake-key')
    result = SimpleNamespace(isError=False, content=[SimpleNamespace(text='[]')])

    async def fake_call_live(name, arguments):
        from backend.xunlongjue.sorftime import _decode
        return _decode(result)

    monkeypatch.setattr(client, '_call_live', fake_call_live)
    import asyncio
    payload = asyncio.run(client.call('category_tree', {'amz_site': 'US', 'node_id': '123'}))
    from backend.xunlongjue.vendor.xlj_core.engine import _normalize_tree_payload
    assert _normalize_tree_payload(payload) == []


def test_realistic_tree_wrapper_accepts_direct_child_fields_without_flattening(monkeypatch):
    client = SorftimeClient('https://mcp.example.test/mcp', 'fake-key')
    result = SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps({
        'data': [{
            'node_id': '123456', 'category_name': 'Storage & Organization',
            'children': [{'node_id': '999999', 'category_name': 'Grandchild'}],
        }],
    }))])

    async def fake_call_live(name, arguments):
        from backend.xunlongjue.sorftime import _decode
        return _decode(result)

    monkeypatch.setattr(client, '_call_live', fake_call_live)
    import asyncio
    payload = asyncio.run(client.call('category_tree', {
        'amz_site': 'US', 'node_id': '12345',
    }))
    from backend.xunlongjue.vendor.xlj_core.engine import _normalize_tree_payload
    assert _normalize_tree_payload(payload) == [
        {'id': '123456', 'name': 'Storage & Organization'},
    ]


def test_tree_payload_rejects_slug_ids_as_non_successful_items():
    from backend.xunlongjue.vendor.xlj_core.engine import _normalize_tree_payload
    with pytest.raises(ValueError, match='纯数字'):
        _normalize_tree_payload({'data': [
            {'node_id': 'home-storage', 'category_name': 'Storage'},
        ]})


def test_mcp_diagnostic_scrubs_api_key_and_url():
    async def fake_transport(tool, arguments):
        raise RuntimeError('failed https://mcp.example.test/?key=very-secret')

    client = SorftimeClient('https://mcp.example.test/mcp', 'very-secret', transport=fake_transport)
    with pytest.raises(RuntimeError) as error:
        import asyncio
        asyncio.run(client.call('category_tree', {'node_id': '123', 'amz_site': 'US'}))
    assert 'very-secret' not in str(error.value)
    assert 'mcp.example.test' not in str(error.value)


def test_create_run_executes_serial_mcp_job_and_only_then_writes_final_report(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    instances = []

    def factory(endpoint, key):
        client = FakeSorftime(endpoint, key)
        instances.append(client)
        return client

    monkeypatch.setattr(service, '_CLIENT_FACTORY', factory)
    result = service.create_run('US', '12345', max_depth=3, max_nodes=10)
    deadline = time.monotonic() + 10
    state = service.get_run(result['run_id'])
    while not state.get('report_url') and time.monotonic() < deadline:
        time.sleep(0.05)
        state = service.get_run(result['run_id'])

    assert state['status'] == 'completed'
    assert state['engine_status'] == 'ALL_DONE'
    assert state['node_id'] == '12345'
    assert state['progress'] == 100
    assert state['max_depth'] == 3
    assert state['max_nodes'] == 10
    assert state['policy']['max_nodes'] == 10
    assert Path(state['report_path']).is_file()
    assert instances[0].calls == [('category_tree', {'amz_site': 'US', 'node_id': '12345'})]
    assert (xlj_home / result['run_id'] / 'workspace' / 'ipc').is_dir()
    test_app = FastAPI()
    test_app.include_router(router)
    with TestClient(test_app) as client:
        report = client.get(state['report_url'])
    assert report.status_code == 200
    assert '<html' in report.text.lower()
    service._write_control(result['run_id'], report_path=str(xlj_home / 'outside.html'))
    with pytest.raises(ValueError, match='报告文件不存在或路径无效'):
        service.report_file(result['run_id'])


def test_run_retry_info_summarizes_backoff_and_scrubs_error(xlj_home):
    settings.save('https://mcp.example.test/mcp', 'very-secret')
    run_id = '12345678-1234-1234-1234-123456789abc'
    workspace = xlj_home / run_id / 'workspace'
    workspace.mkdir(parents=True)
    engine = service._engine()
    engine.create_run(str(workspace), 'US', '12345', '12345', run_id=run_id)
    service._write_control(run_id, status='running', site='US', node_id='12345')
    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    conn = xlj_db.connect(str(workspace))
    try:
        conn.execute(
            "UPDATE jobs SET state='PENDING',attempts=1,eligible_at=?,last_error=?,updated_at=? "
            "WHERE run_id=? AND job_type='category_tree'",
            ('2099-01-01T00:00:00+00:00',
             'failed https://mcp.example.test/?key=very-secret',
             '2026-09-27T00:00:00+00:00', run_id),
        )
        conn.commit()
    finally:
        conn.close()

    info = service.get_run(run_id)['retry_info']
    assert info == {
        'retry_count': 1,
        'waiting_count': 1,
        'exhausted_count': 0,
        'suspended_count': 0,
        'next_retry_at': '2099-01-01T00:00:00+00:00',
        'last_error': 'failed [服务地址已隐藏]',
        'last_node_id': '12345',
        'last_attempt': 1,
        'max_attempts': 3,
    }
    assert 'very-secret' not in json.dumps(info)
    assert 'mcp.example.test' not in json.dumps(info)


def test_worker_setup_error_is_interrupted_and_can_be_resumed(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda *_: (_ for _ in ()).throw(RuntimeError('setup failed')))
    result = service.create_run('US', '67890')
    deadline = time.monotonic() + 5
    state = service.get_run(result['run_id'])
    while state['status'] != 'interrupted' and time.monotonic() < deadline:
        time.sleep(0.05)
        state = service.get_run(result['run_id'])
    assert state['status'] == 'interrupted'

    instances = []
    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda endpoint, key: instances.append(FakeSorftime(endpoint, key)) or instances[-1])
    service.resume(result['run_id'])
    deadline = time.monotonic() + 10
    state = service.get_run(result['run_id'])
    while not state.get('report_url') and time.monotonic() < deadline:
        time.sleep(0.05)
        state = service.get_run(result['run_id'])
    assert state['status'] == 'completed'
    assert instances and instances[0].calls


def test_partial_report_is_staged_and_resume_reopens_suspended_work(xlj_home, monkeypatch):
    run_id = '12345678-1234-1234-1234-123456789abc'
    workspace = xlj_home / run_id / 'workspace'
    workspace.mkdir(parents=True)
    engine = service._engine()
    engine.create_run(str(workspace), 'US', '24680', '24680', run_id=run_id,
                      report_dir=str(xlj_home / run_id / 'reports'))
    service._write_control(run_id, status='pausing', site='US', node_id='24680')
    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    conn = xlj_db.connect(str(workspace))
    try:
        conn.execute("UPDATE nodes SET state='SUSPENDED' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE jobs SET state='CANCELED' WHERE run_id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()

    service._finish_worker_boundary(run_id)
    partial = service.get_run(run_id)
    assert partial['status'] == 'partial_done'
    assert partial['engine_status'] == 'PARTIAL_DONE'
    assert partial['report_url'], (partial, service._read_control(run_id))
    assert partial['report_url'].endswith('/report')
    assert '<html' in service.report_file(run_id).read_text(encoding='utf-8').lower()

    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _: None)
    resumed = service.resume(run_id)
    assert resumed['status'] == 'running'
    assert resumed['report_url'] is None
    assert engine.status(str(workspace), run_id)['node_counts'].get('SUSPENDED', 0) == 0


def test_pause_then_immediate_resume_waits_for_ingest_and_preserves_lease(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    started = threading.Event()
    release = threading.Event()

    class BlockingSorftime(FakeSorftime):
        async def call(self, name, arguments):
            self.calls.append((name, arguments))
            started.set()
            assert release.wait(5)
            return {'items': [{'id': '11223', 'name': 'Child'}]}

    instances = []
    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda endpoint, key: instances.append(BlockingSorftime(endpoint, key)) or instances[-1])
    result = service.create_run('US', '99887')
    assert started.wait(5)

    pausing = service.pause(result['run_id'])
    assert pausing['status'] == 'pausing'
    with pytest.raises(ValueError, match='不能恢复'):
        service.resume(result['run_id'])

    workspace = xlj_home / result['run_id'] / 'workspace'
    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    conn = xlj_db.connect(str(workspace))
    try:
        leased = conn.execute("SELECT state,attempts FROM jobs WHERE job_type='category_tree'").fetchone()
        assert leased['state'] == 'LEASED'
        assert leased['attempts'] == 1
    finally:
        conn.close()

    release.set()
    deadline = time.monotonic() + 5
    state = service.get_run(result['run_id'])
    while state['status'] != 'paused' and time.monotonic() < deadline:
        time.sleep(0.03)
        state = service.get_run(result['run_id'])
    assert state['status'] == 'paused'
    conn = xlj_db.connect(str(workspace))
    try:
        root_job = conn.execute("SELECT state,attempts FROM jobs WHERE job_type='category_tree'").fetchone()
        pending_trends = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='category_trend' AND state='PENDING' AND attempts=0").fetchone()[0]
        assert root_job['state'] == 'SUCCEEDED'
        assert root_job['attempts'] == 1
        assert pending_trends == 2
        assert conn.execute("SELECT COUNT(*) FROM mcp_attempts WHERE run_id=?", (result['run_id'],)).fetchone()[0] == 1
    finally:
        conn.close()

    # Keep the resumed queue unscheduled in this assertion window. The only call
    # observed remains the one whose result was ingested before Resume succeeded.
    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _: None)
    resumed = service.resume(result['run_id'])
    assert resumed['status'] == 'running'
    assert len(instances[0].calls) == 1


def test_pause_on_last_batch_still_generates_final_report(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    started = threading.Event()
    release = threading.Event()

    class BlockingLeafSorftime(FakeSorftime):
        async def call(self, name, arguments):
            self.calls.append((name, arguments))
            started.set()
            assert release.wait(5)
            return {'items': []}

    instances = []
    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda endpoint, key: instances.append(BlockingLeafSorftime(endpoint, key)) or instances[-1])
    result = service.create_run('US', '13579')
    assert started.wait(5)
    pause_result = service.pause(result['run_id'])
    assert pause_result['status'] == 'pausing'
    assert pause_result['activity_status'] == 'pausing'
    release.set()

    deadline = time.monotonic() + 10
    state = service.get_run(result['run_id'])
    while state['status'] != 'completed' and time.monotonic() < deadline:
        time.sleep(0.03)
        state = service.get_run(result['run_id'])
    assert state['status'] == 'completed'
    assert state['engine_status'] == 'ALL_DONE'
    assert state['report_url'].endswith('/report')
    assert service._read_control(result['run_id'])['report_mode'] == 'final'
    assert service.report_file(result['run_id']).is_file()
    assert len(instances[0].calls) == 1


def test_recover_marks_running_as_interrupted_without_resuming_paid_work(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    engine = service._engine()
    workspace = xlj_home / '12345678-1234-1234-1234-123456789abc' / 'workspace'
    run_id = engine.create_run(str(workspace), 'US', '54321', '54321', run_id='12345678-1234-1234-1234-123456789abc')
    (xlj_home / run_id).mkdir(parents=True, exist_ok=True)
    service._write_control(run_id, status='running', site='US', node_id='54321')
    calls = []
    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda *_: calls.append('created'))

    service.recover()

    assert service.get_run(run_id)['status'] == 'interrupted'
    assert calls == []


def test_recover_skips_bad_records_and_reports_safe_warning_summary(xlj_home, monkeypatch, caplog):
    corrupt_id = '11111111-1111-1111-1111-111111111111'
    unreadable_id = '22222222-2222-2222-2222-222222222222'
    write_failed_id = '33333333-3333-3333-3333-333333333333'
    healthy_id = '44444444-4444-4444-4444-444444444444'

    corrupt_dir = xlj_home / corrupt_id
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / 'control.json').write_text('{broken', encoding='utf-8')

    unreadable_dir = xlj_home / unreadable_id
    unreadable_dir.mkdir()
    (unreadable_dir / 'control.json').mkdir()

    for run_id in (write_failed_id, healthy_id):
        (xlj_home / run_id).mkdir()
        service._write_control(run_id, status='running', site='US', node_id='123')

    original_write_control = service._write_control

    def fail_one_control_write(run_id, **fields):
        if run_id == write_failed_id:
            raise PermissionError('secret path and private-key-value')
        return original_write_control(run_id, **fields)

    monkeypatch.setattr(service, '_write_control', fail_one_control_write)
    service.recover()

    assert (corrupt_dir / 'control.json').read_text(encoding='utf-8') == '{broken'
    assert (xlj_home / write_failed_id / 'control.json').exists()
    assert service._read_control(healthy_id)['status'] == 'interrupted'
    assert service.recovery_warnings() == {
        'count': 3,
        'items': [
            {'run_id': corrupt_id, 'reason': 'invalid_control'},
            {'run_id': unreadable_id, 'reason': 'read_failed'},
            {'run_id': write_failed_id, 'reason': 'write_failed'},
        ],
    }
    assert 'private-key-value' not in caplog.text
    assert 'secret path' not in caplog.text

    test_app = FastAPI()
    test_app.include_router(router)
    with TestClient(test_app) as client:
        response = client.get('/api/xunlongjue/runs')
    assert response.status_code == 200
    assert response.json()['recovery_warnings']['count'] == 3


def test_http_contract_does_not_return_key(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    test_app = FastAPI()
    test_app.include_router(router)
    with TestClient(test_app) as client:
        response = client.get('/api/xunlongjue/settings')
    assert response.status_code == 200
    assert response.json() == {'endpoint': 'https://mcp.example.test/mcp', 'key_configured': True}
    assert 'fake-key' not in response.text
