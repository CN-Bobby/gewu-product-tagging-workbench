import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.xunlongjue import router, service, settings
from backend.xunlongjue.sorftime import SorftimeClient, scrub


class FakeSorftime:
    def __init__(self, endpoint, api_key):
        self.endpoint = endpoint
        self.api_key = api_key
        self.calls = []

    async def call(self, name, arguments, *, audit_context=None):
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
    service.stop_all()
    worker = service._THREAD
    if worker and worker.is_alive():
        worker.join(timeout=15)
    if worker and worker.is_alive():
        raise RuntimeError('Previous Xunlongjue test worker did not stop; refusing to share its state.')
    monkeypatch.setattr(settings, '_root', lambda: tmp_path / 'xunlongjue')
    monkeypatch.setattr(service, 'root', lambda: tmp_path / 'xunlongjue')
    yield tmp_path / 'xunlongjue'
    service.stop_all()
    worker = service._THREAD
    if worker and worker.is_alive():
        worker.join(timeout=15)
    if worker and worker.is_alive():
        raise RuntimeError('Xunlongjue test worker did not stop before fixture cleanup.')


def test_settings_separate_endpoint_and_key_and_reject_query_secrets(xlj_home):
    from backend.credential_vault import get_secret
    assert settings.save('https://mcp.example.test/mcp', 'secret-key-value') == {
        'endpoint': 'https://mcp.example.test/mcp', 'key_configured': True,
    }
    persisted = json.loads((xlj_home / 'settings.json').read_text(encoding='utf-8'))
    assert persisted == {'endpoint': 'https://mcp.example.test/mcp'}
    assert get_secret('sorftime','api_key') == 'secret-key-value'
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
        'job_id': 'job-1', 'attempt': 2,
        'indicator': 'SalesCount',
        'arguments': {'amz_site': 'US', 'node_id': '123', 'trend_index': 'SalesCount'},
    }, 'run-1')
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


def test_tree_no_data_context_requires_same_run_parent_and_completed_trends(xlj_home, monkeypatch):
    import importlib
    import uuid

    run_id = str(uuid.uuid4())
    workspace = service._workspace(run_id)
    engine = service._engine()
    engine.create_run(
        str(workspace), 'US', '100', 'Root', run_id=run_id,
    )
    db_module = importlib.import_module(
        '.vendor.xlj_core.db', package='backend.xunlongjue',
    )
    conn = db_module.connect(str(workspace))
    try:
        now = db_module.utc_now()
        conn.execute(
            "INSERT INTO nodes(run_id,node_id,path,level,state,result,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run_id, '200', 'Root/Child', 2, 'SUSPENDED', 'MCP_OR_DATA_FAILED', now, now),
        )
        conn.execute(
            "INSERT INTO edges(run_id,parent_id,child_id,child_name,created_at) VALUES(?,?,?,?,?)",
            (run_id, '100', '200', 'Child', now),
        )
        for indicator in ('SalesCount', 'AvgPrice'):
            conn.execute(
                "INSERT INTO jobs(job_id,run_id,node_id,job_type,indicator,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f'{indicator}-job', run_id, '200', 'category_trend', indicator,
                 'SUCCEEDED', now, now),
            )
        conn.execute(
            "INSERT INTO jobs(job_id,run_id,node_id,job_type,indicator,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ('tree-job', run_id, '200', 'category_tree', '', 'LEASED', now, now),
        )
        conn.commit()
    finally:
        conn.close()

    context = service._tree_no_data_context(run_id, {
        'job_id': 'tree-job', 'node_id': '200',
    })
    assert context == {
        'is_root': False,
        'node_known': True,
        'parent_edge_found': True,
        'tree_job_leased': True,
        'successful_trend_count': 2,
    }
    assert service._tree_no_data_context(run_id, {
        'job_id': 'tree-job', 'node_id': '100',
    })['is_root'] is True
    orphan_run_id = str(uuid.uuid4())
    orphan_context = service._tree_no_data_context(orphan_run_id, {
        'job_id': 'tree-job', 'node_id': '200',
    })
    assert orphan_context['is_root'] is None
    from backend.xunlongjue.sorftime import _no_data_tree_is_eligible
    assert not _no_data_tree_is_eligible('category_tree', orphan_context)

    # Exercise service context -> real SorftimeClient parser -> wire audit as
    # one path; a fake MCP transport keeps this fully offline.
    from backend.xunlongjue.sorftime import SorftimeClient, _LiveCallResult

    audit_dir = xlj_home / 'integration-exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))

    async def transport(name, arguments):
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text='No data available.')],
            structuredContent=None,
        )
        return _LiveCallResult(result, {
            'is_error': False,
            'raw_response_text': 'No data available.',
            'http_responses': [{'method': 'POST', 'status_code': 200}],
        })

    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'adapter-test-secret', transport=transport,
    )
    job = {
        'type': 'category_tree', 'site': 'US', 'node_id': '200',
        'job_id': 'tree-job', 'attempt': 1,
    }
    assert service._call_for_job(client, job, run_id) == {'data': []}

    with pytest.raises(RuntimeError, match='child structure is unconfirmed'):
        service._call_for_job(client, {
            **job, 'job_id': 'orphan-tree-job',
        }, orphan_run_id)

    records = [json.loads(path.read_text(encoding='utf-8'))
               for path in sorted(audit_dir.glob('exchange-*.json'))]
    assert [record['status'] for record in records] == ['success', 'failed']
    accepted_wire = records[0]['wire_response']
    assert accepted_wire['parse_stage'] == 'no_data_sentinel_as_empty_child_array'
    assert accepted_wire['official_sorftime_semantics_confirmed'] is False
    assert accepted_wire['http_responses'][0]['status_code'] == 200
    rejected_wire = records[1]['wire_response']
    assert rejected_wire['parse_stage'] == 'no_data_sentinel_unconfirmed'
    assert 'adapter-test-secret' not in json.dumps(records, ensure_ascii=False)


def test_sorftime_trend_envelope_keeps_latest_24_complete_consecutive_points(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start_index = (now.year - 2) * 12 + now.month - 1
    raw = []
    for offset in range(25):
        index = start_index + offset
        year, month = divmod(index, 12)
        raw.append(f'{year:04d}-{month + 1:02d}={offset + 1}.5')
    # The final row is the current, incomplete month and must be excluded.
    normalized = service._normalize_sorftime_trend({'doc': {}, 'data': raw}, 'SalesCount')
    assert len(normalized['months']) == 24
    assert normalized['values'][0] == 1.5
    assert normalized['values'][-1] == 24.5
    assert normalized['months'][-1] < now.strftime('%Y-%m')


def test_sorftime_trend_envelope_rejects_nonfinite_complete_month_points():
    raw = {'data': [
        '2026-01=1', '2026-02=nan', '2026-03=3', '2026-04=4',
        '2026-05=5', '2026-06=6', '2026-07=7', '2026-08=8',
    ]}
    with pytest.raises(ValueError, match='有限数字'):
        service._normalize_sorftime_trend(raw, 'AvgPrice')


def test_sorftime_trend_envelope_rejects_complete_month_gaps():
    with pytest.raises(ValueError, match='必须连续'):
        service._normalize_sorftime_trend({'data': [
            '2026-01=1', '2026-02=2', '2026-04=4', '2026-05=5',
            '2026-06=6', '2026-07=7', '2026-08=8',
        ]}, 'SalesCount')


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


def test_sorftime_prefers_text_json_when_structured_content_is_also_present():
    from backend.xunlongjue.sorftime import _decode
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text='{"data":[]}')],
        structuredContent={'data': [{'node_id': '999', 'category_name': 'ignored'}]},
    )
    assert _decode(result) == {'data': []}


def test_sorftime_uses_structured_content_only_when_text_is_empty():
    from backend.xunlongjue.sorftime import _decode
    result = SimpleNamespace(isError=False, content=[], structuredContent={'data': []})
    assert _decode(result) == {'data': []}


def test_sorftime_decodes_the_installed_mcp_result_model():
    from mcp.types import CallToolResult
    from backend.xunlongjue.sorftime import _decode
    result = CallToolResult(content=[], structuredContent={'data': []})
    assert _decode(result) == {'data': []}


def test_sorftime_does_not_fall_back_to_structured_content_for_non_json_text():
    from backend.xunlongjue.sorftime import SorftimeDecodeError, _decode
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text='No data available.')],
        structuredContent={'data': []},
    )
    with pytest.raises(SorftimeDecodeError, match='child structure is unconfirmed') as error:
        _decode(result)
    assert error.value.stage == 'no_data_sentinel_unconfirmed'
    assert error.value.raw_text == 'No data available.'

    with pytest.raises(SorftimeDecodeError, match='category_trend returned no data') as trend_error:
        _decode(result, tool_name='category_trend')
    assert trend_error.value.stage == 'no_data_response_unconfirmed'


def test_sorftime_no_data_sentinel_maps_only_after_verified_child_context():
    from backend.xunlongjue.sorftime import (
        _decode, _no_data_tree_is_eligible,
    )

    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text='No data available.')],
        structuredContent=None,
    )
    verified = {
        'is_root': False,
        'node_known': True,
        'parent_edge_found': True,
        'tree_job_leased': True,
        'successful_trend_count': 2,
    }
    assert _no_data_tree_is_eligible('category_tree', verified)
    wire = {'raw_response_text': 'No data available.'}
    assert _decode(
        result, tool_name='category_tree', allow_no_data_as_empty=True,
        wire_response=wire,
    ) == []
    assert wire['parse_stage'] == 'no_data_sentinel_as_empty_child_array'
    assert wire['normalization'] == 'exact_text_to_empty_direct_children_for_verified_non_root_node'

    for changes in (
        {'is_root': True},
        {'node_known': False},
        {'parent_edge_found': False},
        {'tree_job_leased': False},
        {'successful_trend_count': 1},
    ):
        invalid = {**verified, **changes}
        assert not _no_data_tree_is_eligible('category_tree', invalid)

    assert not _no_data_tree_is_eligible('category_trend', verified)


def test_sorftime_preserves_plain_text_timeout_diagnostic_without_secrets():
    from backend.xunlongjue.sorftime import _decode
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text=(
            'Soft timeout while reading https://mcp.example.test/?key=very-secret'
        ))],
        structuredContent={'data': []},
    )
    with pytest.raises(ValueError) as error:
        _decode(result, key='very-secret')
    assert 'Soft timeout while reading' in str(error.value)
    assert 'very-secret' not in str(error.value)
    assert 'mcp.example.test' not in str(error.value)


def test_sorftime_mcp_is_error_remains_failure_and_is_scrubbed():
    from backend.xunlongjue.sorftime import _decode
    result = SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(text='failed https://mcp.example.test/?key=very-secret')],
        structuredContent={'data': []},
    )
    with pytest.raises(RuntimeError) as error:
        _decode(result, key='very-secret')
    assert 'failed' in str(error.value)
    assert 'very-secret' not in str(error.value)
    assert 'mcp.example.test' not in str(error.value)


def test_sorftime_call_enforces_injectable_total_timeout(monkeypatch):
    assert SorftimeClient('https://mcp.example.test/mcp', 'fake-key').timeout_seconds == 180
    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'fake-key', timeout_seconds=0.01,
    )

    async def slow_live_call(name, arguments):
        # Represents any delay inside connect, initialize, or call_tool.
        await asyncio.sleep(1)

    monkeypatch.setattr(client, '_call_live', slow_live_call)
    with pytest.raises(RuntimeError, match='超过 0.01 秒总期限'):
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
        ))


def test_sorftime_exchange_audit_is_opt_in_contextual_and_redacted(tmp_path, monkeypatch):
    from backend.xunlongjue.sorftime import SorftimeClient

    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))

    async def fake_transport(name, arguments):
        return {'data': [{'month': '2026-01', 'value': 12,
                          'description': 'contains temporary-secret',
                          'api_key': 'field-secret',
                          'x_api_key': 'prefixed-field-secret',
                          'cookie': 'session-cookie-secret',
                          'cookie_header': 'session-header-secret',
                          'Authorization_Header': 'Bearer auth-header-secret',
                          'month_key': '2026-01',
                          'source': 'https://user:pass@example.test/?key=query-secret&access_token=url-token'}]}

    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'temporary-secret', transport=fake_transport,
    )
    payload = asyncio.run(client.call(
        'category_trend',
        {'amz_site': 'US', 'node_id': '123', 'trend_index': 'SalesCount'},
        audit_context={
            'run_id': 'run-1', 'job_id': 'job-1', 'node_id': '123',
            'site': 'US', 'job_type': 'category_trend',
            'indicator': 'SalesCount', 'attempt': 2,
            'cookie': 'context-cookie-secret',
        },
    ))

    assert payload['data'][0]['value'] == 12
    records = list(audit_dir.glob('exchange-*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['tool'] == 'category_trend'
    assert record['request_arguments']['node_id'] == '123'
    assert record['execution_context']['run_id'] == 'run-1'
    assert record['execution_context']['attempt'] == 2
    assert record['execution_context']['cookie'] == '[redacted]'
    assert record['raw_payload']['data'][0]['api_key'] == '[redacted]'
    assert record['raw_payload']['data'][0]['month_key'] == '[redacted]'
    assert record['raw_payload']['data'][0]['source'] == '[服务地址已隐藏]'
    rendered = records[0].read_text(encoding='utf-8')
    for secret in ('temporary-secret', 'field-secret', 'prefixed-field-secret',
                   'query-secret', 'url-token', 'session-cookie-secret',
                   'context-cookie-secret', 'session-header-secret',
                   'auth-header-secret', 'user:pass', 'example.test'):
        assert secret not in rendered


def test_sorftime_exchange_audit_preserves_redacted_wire_response(tmp_path, monkeypatch):
    from backend.xunlongjue.sorftime import _record_exchange

    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))
    wire = {
        'is_error': False,
        'raw_response_text': '{"api_key":"wire-secret","value":7}',
        'http_responses': [{'method': 'POST', 'status_code': 200,
                            'content_type': 'text/event-stream'}],
    }
    _record_exchange(
        'category_tree', {'amz_site': 'US', 'node_id': '123'}, status='failed',
        payload=None, error='unconfirmed tree', elapsed_seconds=1.0,
        api_key='wire-secret', context={'run_id': 'run-1'},
        started_at='2026-09-29T00:00:00Z', wire_response=wire,
    )
    records = list(audit_dir.glob('exchange-*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['wire_response']['raw_response_text'] == '{"api_key": "[redacted]", "value": 7}'
    assert record['wire_response']['http_responses'][0]['status_code'] == 200
    assert 'wire-secret' not in records[0].read_text(encoding='utf-8')


def test_sorftime_exchange_keeps_http_status_when_transport_fails(tmp_path, monkeypatch):
    from backend.xunlongjue import sorftime

    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))
    client = SorftimeClient('https://mcp.example.test/mcp', 'fake-key')

    async def fail_after_http_response(name, arguments):
        response_audit = sorftime._LIVE_RESPONSE_AUDIT.get()
        response_audit['http_responses'] = []
        response_audit['http_responses'].append({
            'method': 'POST', 'status_code': 503, 'content_type': 'application/json',
        })
        raise OSError('upstream returned 503')

    client._call_live = fail_after_http_response
    with pytest.raises(RuntimeError, match='503'):
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
        ))

    record = json.loads(next(audit_dir.glob('exchange-*.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'failed'
    assert record['wire_response']['capture_stage'] == 'mcp_transport_failed_after_http_response'
    assert record['wire_response']['http_responses'][0]['status_code'] == 503


def test_sorftime_exchange_records_explicit_cancellation(tmp_path, monkeypatch):
    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))

    async def blocked_transport(name, arguments):
        await asyncio.Future()

    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'fake-key', transport=blocked_transport,
    )

    async def cancel_call():
        task = asyncio.create_task(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())
    record = json.loads(next(audit_dir.glob('exchange-*.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'cancelled'
    assert record['error_redacted'] == 'Sorftime MCP 调用已取消'


def test_sorftime_exchange_audit_records_soft_timeout_error_redacted(tmp_path, monkeypatch):
    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))

    async def timeout_transport(name, arguments):
        raise RuntimeError(
            'Soft timeout at https://mcp.example.test/?key=temporary-secret'
        )

    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'temporary-secret', transport=timeout_transport,
    )
    with pytest.raises(RuntimeError, match='Soft timeout'):
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
            audit_context={'run_id': 'run-2', 'node_id': '123', 'attempt': 1},
        ))

    records = list(audit_dir.glob('exchange-*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['status'] == 'failed'
    assert 'Soft timeout' in record['error_redacted']
    assert 'temporary-secret' not in records[0].read_text(encoding='utf-8')


def test_sorftime_live_http_hook_captures_plain_text_soft_timeout(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from backend.xunlongjue import sorftime
    import mcp.shared._httpx_utils as httpx_utils

    audit_dir = tmp_path / 'exchanges'
    monkeypatch.setenv('XLJ_SORFTIME_EXCHANGE_DIR', str(audit_dir))

    class FakeHTTPClient:
        event_hooks = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(_endpoint, *, http_client, terminate_on_close):
        assert terminate_on_close is False
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            self.http_client = fake_http_client

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _name, *, arguments):
            assert arguments == {'amz_site': 'US', 'node_id': '123'}
            response = SimpleNamespace(
                request=SimpleNamespace(method='POST'),
                status_code=200,
                headers={'content-type': 'text/event-stream'},
            )
            await self.http_client.event_hooks['response'][0](response)
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(
                    text='Soft timeout while reading https://mcp.example.test/?key=temporary-secret',
                )],
                structuredContent=None,
            )

    fake_http_client = FakeHTTPClient()
    monkeypatch.setattr(httpx_utils, 'create_mcp_http_client', lambda: fake_http_client)
    monkeypatch.setattr(sorftime, 'streamable_http_client', fake_streamable_http_client)
    monkeypatch.setattr(sorftime, 'ClientSession', FakeSession)
    client = SorftimeClient('https://mcp.example.test/mcp', 'temporary-secret')

    with pytest.raises(RuntimeError, match='Soft timeout') as error:
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
            audit_context={'run_id': 'run-live-hook', 'attempt': 1},
        ))

    assert 'temporary-secret' not in str(error.value)
    record_path = next(audit_dir.glob('exchange-*.json'))
    rendered = record_path.read_text(encoding='utf-8')
    record = json.loads(rendered)
    assert record['status'] == 'failed'
    assert record['wire_response']['parse_stage'] == 'parse_tool_text_json'
    assert record['wire_response']['http_responses'] == [{
        'sequence': 1,
        'method': 'POST',
        'status_code': 200,
        'content_type': 'text/event-stream',
    }]
    assert 'Soft timeout' in record['error_redacted']
    assert 'temporary-secret' not in rendered


def test_sorftime_rejects_payload_returned_after_timeout_was_suppressed(monkeypatch):
    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'fake-key', timeout_seconds=0.01,
    )

    async def suppress_cancellation(name, arguments):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # A transport that swallows cancellation must not turn an expired
            # request back into a successful category tree.
            return {'data': [{'node_id': '123456', 'category_name': 'Late'}]}

    monkeypatch.setattr(client, '_call_live', suppress_cancellation)
    with pytest.raises(RuntimeError, match='超过 0.01 秒总期限'):
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
        ))


def test_sorftime_total_timeout_is_clear_when_mcp_wraps_cancel_in_task_group(monkeypatch):
    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'fake-key', timeout_seconds=0.01,
    )

    async def task_group_cleanup(name, arguments):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # AnyIO/MCP transport cleanup can surface an exception group after
            # the outer asyncio deadline cancels the request.
            raise ExceptionGroup(
                'unhandled errors in a TaskGroup',
                [TimeoutError('SSE stream was closed')],
            ) from None

    monkeypatch.setattr(client, '_call_live', task_group_cleanup)
    with pytest.raises(RuntimeError, match='超过 0.01 秒总期限') as error:
        asyncio.run(client.call(
            'category_tree', {'amz_site': 'US', 'node_id': '123'},
        ))
    assert 'SSE stream was closed' in str(error.value)


def test_sorftime_preserves_transport_timeout_cause():
    async def fail_with_read_timeout(name, arguments):
        raise TimeoutError('SSE read timed out')

    client = SorftimeClient(
        'https://mcp.example.test/mcp', 'fake-key',
        transport=fail_with_read_timeout, timeout_seconds=1,
    )
    with pytest.raises(RuntimeError) as error:
        asyncio.run(client.call('category_tree', {'amz_site': 'US', 'node_id': '123'}))
    assert 'SSE read timed out' in str(error.value)
    assert '总期限' not in str(error.value)


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


def test_mcp_diagnostic_keeps_safe_leaf_causes_from_exception_group():
    error = ExceptionGroup('unhandled errors in a TaskGroup', [
        ConnectionResetError('connection reset by peer'),
        ExceptionGroup('nested task group', [
            TimeoutError('read timed out at https://mcp.example.test/?key=very-secret'),
        ]),
    ])
    diagnostic = scrub(error, 'very-secret')
    assert 'ConnectionResetError: connection reset by peer' in diagnostic
    assert 'TimeoutError: read timed out' in diagnostic
    assert 'TaskGroup' not in diagnostic
    assert 'very-secret' not in diagnostic
    assert 'mcp.example.test' not in diagnostic


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
    service._write_control(result['run_id'], report_path='')
    empty_legacy = service.get_run(result['run_id'])
    assert empty_legacy['needs_rerun'] is True
    assert empty_legacy['report_needs_rerun'] is True
    assert empty_legacy['report_url'] is None
    service._write_control(result['run_id'], report_path=str(xlj_home / 'outside.html'))
    with pytest.raises(ValueError, match='报告文件不存在或路径无效'):
        service.report_file(result['run_id'])


def test_full_run_policy_metadata_filters_presets_and_active_bulk_guard(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _run_id: None)
    app = FastAPI()
    app.include_router(router)
    policy = {
        'min_avg_sales': 4200,
        'max_level': 7,
        'sales_decline_tolerance': 0.08,
        'price_decline_tolerance': 0.12,
        'min_r_squared': 0.25,
        'batch_size': 9,
        'max_attempts': 4,
        'max_data_lag_months': 1,
    }
    with TestClient(app) as client:
        created = client.post('/api/xunlongjue/runs', json={
            'site': 'US', 'node_id': '321', 'category': 'Storage',
            'operator': 'Ada', 'policy': policy, 'max_nodes': 50,
        })
        assert created.status_code == 200
        run_id = created.json()['run_id']
        detail = client.get(f'/api/xunlongjue/runs/{run_id}').json()
        assert detail['policy']['batch_size'] == 9
        assert detail['policy']['max_level'] == 7
        assert detail['category'] == 'Storage'
        assert detail['operator'] == 'Ada'
        assert detail['revision'] == 1

        changed = client.post(f'/api/xunlongjue/runs/{run_id}/metadata', json={
            'revision': 1, 'name': 'Spring scan', 'note': 'Review the tree',
        })
        assert changed.status_code == 200
        assert changed.json()['revision'] == 2
        assert changed.json()['name'] == 'Spring scan'
        assert changed.json()['note'] == 'Review the tree'
        stale = client.post(f'/api/xunlongjue/runs/{run_id}/metadata', json={
            'revision': 1, 'name': 'stale',
        })
        assert stale.status_code == 409

        filtered = client.get('/api/xunlongjue/runs', params={
            'search': 'Spring', 'status': 'running', 'sort': 'name', 'direction': 'asc',
            'archived': 'false', 'trashed': 'false',
        }).json()['runs']
        assert [item['id'] for item in filtered] == [run_id]
        blocked = client.post('/api/xunlongjue/runs/bulk', json={
            'action': 'archive', 'run_ids': [run_id],
        })
        assert blocked.status_code == 400
        assert '活动运行' in blocked.json()['detail']
        assert service.pause(run_id)['status'] == 'paused'
        assert service.bulk_runs('archive', [run_id])['runs'][0]['archived'] is True
        assert len(service.list_runs(archived=True, trashed=False)) == 1
        trashed = service.bulk_runs('trash', [run_id])['runs'][0]
        assert trashed['trashed'] is True and trashed['archived'] is False
        assert len(service.list_runs(archived=False, trashed=True)) == 1
        restored = service.bulk_runs('restore', [run_id])['runs'][0]
        assert restored['trashed'] is False and restored['archived'] is False

        preset = client.post('/api/xunlongjue/presets', json={
            'name': 'Balanced', 'policy': policy,
        })
        assert preset.status_code == 200
        preset_id = preset.json()['preset_id']
        listed = client.get('/api/xunlongjue/presets').json()['presets']
        assert listed[0]['policy']['batch_size'] == 9
        updated = client.put(f'/api/xunlongjue/presets/{preset_id}', json={'name': 'Fast'})
        assert updated.status_code == 200
        assert updated.json()['name'] == 'Fast'
        assert client.delete(f'/api/xunlongjue/presets/{preset_id}').json()['deleted'] is True


def test_node_cap_rejects_tree_without_clipping_to_leaf_or_opportunity(xlj_home, monkeypatch):
    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _run_id: None)
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    run = service.create_run('US', '12345', max_nodes=1)
    tree = {'data': [{'node_id': '67890', 'category_name': 'Child'}]}
    with pytest.raises(service.NodeCapReached, match='未截断'):
        service._cap_tree_result(run['run_id'], tree)
    assert tree['data'] == [{'node_id': '67890', 'category_name': 'Child'}]


def test_empty_legacy_terminal_report_requires_rerun_but_verified_empty_root_does_not(xlj_home, monkeypatch):
    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _run_id: None)
    run = service.create_run('US', '24680')
    workspace = str(xlj_home / run['run_id'] / 'workspace')
    report = xlj_home / run['run_id'] / 'reports' / 'old-empty.html'
    report.write_text('<html>empty historical report</html>', encoding='utf-8')
    conn = xlj_db.connect(workspace)
    try:
        with xlj_db.transaction(conn):
            conn.execute(
                "UPDATE nodes SET state='EXPANDED',result='EXPANDED' WHERE run_id=? AND node_id='24680'",
                (run['run_id'],),
            )
            conn.execute(
                "UPDATE jobs SET state='SUCCEEDED' WHERE run_id=? AND node_id='24680' AND job_type='category_tree'",
                (run['run_id'],),
            )
    finally:
        conn.close()
    service._write_control(run['run_id'], status='all_done', report_path=str(report), report_mode='final')

    empty_without_evidence = service.get_run(run['run_id'])
    assert empty_without_evidence['needs_rerun'] is True
    assert '没有有效的量价判断' in empty_without_evidence['needs_rerun_reason']
    assert empty_without_evidence['report_url'] is None

    conn = xlj_db.connect(workspace)
    try:
        with xlj_db.transaction(conn):
            conn.execute(
                "UPDATE nodes SET result='ROOT_EMPTY' WHERE run_id=? AND node_id='24680'",
                (run['run_id'],),
            )
    finally:
        conn.close()
    verified_empty = service.get_run(run['run_id'])
    assert verified_empty['needs_rerun'] is False
    assert verified_empty['report_url'].endswith('/report')


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
    assert partial['needs_rerun'] is True
    assert partial['report_url'] is None
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
        async def call(self, name, arguments, *, audit_context=None):
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


def test_batch_progress_does_not_double_count_failed_requests(xlj_home, monkeypatch):
    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    monkeypatch.setattr(service, 'start', lambda: None)
    monkeypatch.setattr(service, '_schedule', lambda _: None)
    service._STOP.clear()
    class FailingSorftime(FakeSorftime):
        async def call(self, name, arguments, *, audit_context=None):
            self.calls.append((name, arguments))
            raise RuntimeError('recorded offline failure')

    run = service.create_run('US', '12345', policy={'batch_size': 4})
    engine = service._engine()
    workspace = str(xlj_home / run['run_id'] / 'workspace')
    client = FailingSorftime('https://mcp.example.test/mcp', 'fake-key')
    batch = engine.lease_jobs(workspace, run['run_id'], limit=4)
    assert len(batch['jobs']) == 1
    service._process_batch(run['run_id'], batch, client)

    conn = xlj_db.connect(workspace)
    try:
        assert conn.execute(
            'SELECT COUNT(*) FROM mcp_attempts WHERE run_id=?', (run['run_id'],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id=? AND state='PENDING' AND attempts=1",
            (run['run_id'],),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_pause_ingests_completed_batch_results_and_releases_unstarted_jobs(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    started = threading.Event()
    release = threading.Event()
    now = time.gmtime()
    latest_index = now.tm_year * 12 + now.tm_mon - 2
    months = []
    for index in range(latest_index - 23, latest_index + 1):
        year, month = divmod(index, 12)
        months.append(f'{year:04d}-{month + 1:02d}')

    class BatchSorftime(FakeSorftime):
        async def call(self, name, arguments, *, audit_context=None):
            self.calls.append((name, arguments))
            if name == 'category_tree':
                return {'data': [
                    {'node_id': '45601', 'category_name': 'One'},
                    {'node_id': '45602', 'category_name': 'Two'},
                ]}
            started.set()
            assert release.wait(5)
            values = [12000] * 24 if arguments.get('trend_index') == 'SalesCount' else [20] * 24
            return {'months': months, 'values': values}

    monkeypatch.setattr(service, '_CLIENT_FACTORY', lambda endpoint, key: BatchSorftime(endpoint, key))
    run = service.create_run('US', '12345', category='Root', policy={'batch_size': 2})
    assert started.wait(5)
    assert service.pause(run['run_id'])['status'] == 'pausing'
    release.set()
    deadline = time.monotonic() + 8
    state = service.get_run(run['run_id'])
    while state['status'] != 'paused' and time.monotonic() < deadline:
        time.sleep(0.03)
        state = service.get_run(run['run_id'])
    assert state['status'] == 'paused'

    from backend.xunlongjue.vendor.xlj_core import db as xlj_db
    conn = xlj_db.connect(str(xlj_home / run['run_id'] / 'workspace'))
    try:
        jobs = conn.execute("SELECT job_type,state,attempts FROM jobs WHERE job_type='category_trend'").fetchall()
        assert sum(row['state'] == 'SUCCEEDED' for row in jobs) == 1
        assert sum(row['state'] == 'PENDING' and row['attempts'] == 0 for row in jobs) == 3
        assert conn.execute("SELECT COUNT(*) FROM mcp_attempts WHERE run_id=?", (run['run_id'],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM batches WHERE run_id=? AND state='LEASED'", (run['run_id'],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_pause_on_last_batch_still_generates_final_report(xlj_home, monkeypatch):
    settings.save('https://mcp.example.test/mcp', 'fake-key')
    started = threading.Event()
    release = threading.Event()

    class BlockingLeafSorftime(FakeSorftime):
        async def call(self, name, arguments, *, audit_context=None):
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
