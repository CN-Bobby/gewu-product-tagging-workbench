"""关键词调研的运行登记、事件流与 HTTP 面。

离线：不起线程、不碰网络，凭据目录指向临时 home，因此绝不读写真实 Reasonix 配置。
引擎本身的端到端由 test_keyword_research_pipeline.py 覆盖；这里测的是工作台自己的那一层，
所以 Pipeline 用桩替换——它只按接口行事：构造、run()、on_event、store.set_phase/log。
"""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.keyword_research import credentials, registry, runner, stream
from backend.keyword_research.input_validator import MARKETPLACES

def _synthetic_asin(serial: int) -> str:
    """合成 ASIN：仓库审计禁止 ASIN 形状的字面量，测试值在运行时拼出来。"""
    return 'B0' + 'SYN%05d' % serial


ASINS = [_synthetic_asin(1), _synthetic_asin(2)]

# 录制夹具只在本机存在，不入版本库（仓库审计禁止 ASIN 形状字面量与模型响应原文入库）。
_FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'keyword_research' / 'mcp_responses'
needs_fixtures = pytest.mark.skipif(
    not _FIXTURES.is_dir() or not any(_FIXTURES.glob('*.json')),
    reason='本机没有真实录制夹具（不入库），离线端到端跳过')


@pytest.fixture(autouse=True)
def isolated_provider_home(monkeypatch, tmp_path):
    """凭据一律指向临时 home：测试不得读真实 provider 配置或密钥。"""
    monkeypatch.setenv('TAG_PROVIDER_HOME', str(tmp_path / 'provider-home'))


@pytest.fixture(autouse=True)
def clean_secret():
    """每个用例从「未配置密钥」开始，断言不必依赖用例顺序。"""
    credentials.clear_secret()
    yield
    credentials.clear_secret()


@pytest.fixture
def client():
    with TestClient(app_module.app) as test_client:
        yield test_client


def _task_input():
    return {'groups': [{'marketplace': 'US', 'asins': ASINS,
                        'node_ids': [], 'node_top_n': 50}],
            'options': {'data_month': '202606', 'matrix_enabled': True}}


def _make_run(**plan_extra):
    run_id = registry.create(_task_input(), {'confirmed': True, 'budget': {'planned_total': 15},
                                              **plan_extra})
    # Unit-level runner fixture starts from the old queued state without exercising queue events.
    registry.write(run_id, status=registry.QUEUED)
    return run_id


# --- 运行登记 -------------------------------------------------------------

def test_create_lays_out_run_directory():
    run_id = _make_run()
    path = registry.run_dir(run_id)
    for name in ('raw', 'cache', 'logs', 'output'):
        assert (path / name).is_dir()
    state = registry.read(run_id)
    assert state['status'] == registry.QUEUED
    assert registry.input_of(run_id)['groups'][0]['marketplace'] == 'US'
    assert registry.plan_of(run_id)['confirmed'] is True


def test_run_id_must_be_our_shape():
    for bad in ('../../etc', 'deadbeef', '', 'x' * 32):
        with pytest.raises(ValueError):
            registry.run_dir(bad)


def test_events_are_monotonic_and_resumable():
    run_id = _make_run()
    for index in range(3):
        assert registry.append_event(run_id, 'log', {'line': str(index)})['sequence'] == index + 1
    assert registry.latest_sequence(run_id) == 3
    tail = registry.read_events(run_id, after_sequence=1)
    assert [event['sequence'] for event in tail] == [2, 3]
    assert registry.read_events(run_id, after_sequence=3) == []


def test_write_merges_fields_and_stamps_update():
    run_id = _make_run()
    before = registry.read(run_id)
    state = registry.write(run_id, stage='collecting', message='开始数据采集')
    assert state['stage'] == 'collecting'
    assert state['status'] == registry.QUEUED
    assert state['created_at'] == before['created_at']
    assert state['updated_at'] >= before['updated_at']


def test_artifact_path_only_serves_registered_names():
    run_id = _make_run()
    path = registry.run_dir(run_id) / 'output'
    (path / '报告.xlsx').write_bytes(b'xlsx')
    registry.write(run_id, artifacts=[{'name': '报告.xlsx', 'bytes': 4, 'sha256': 'a'}])
    assert registry.artifact_path(run_id, '报告.xlsx').name == '报告.xlsx'
    for bad in ('../run.json', 'sub/报告.xlsx', '别的.xlsx', '..'):
        with pytest.raises(ValueError):
            registry.artifact_path(run_id, bad)


def test_recover_marks_active_runs_interrupted_only():
    active = _make_run()
    finished = _make_run()
    registry.write(active, status=registry.RUNNING)
    registry.write(finished, status=registry.DONE)
    registry.recover()
    assert registry.read(active)['status'] == registry.INTERRUPTED
    assert registry.read(active)['error']
    assert [event['event_type'] for event in registry.read_events(active)] == ['run_failed']
    assert registry.read(finished)['status'] == registry.DONE
    assert registry.read_events(finished) == []


# --- 事件流协议 -----------------------------------------------------------

def test_cursor_rules():
    assert stream.resolve_cursor(None, None) == 0
    assert stream.resolve_cursor('7', None) == 7
    assert stream.resolve_cursor(None, '9') == 9
    assert stream.resolve_cursor('7', '9') == 7
    for bad in ('x', '-1', '1.5'):
        with pytest.raises(ValueError):
            stream.resolve_cursor(bad, None)


def test_frame_carries_sequence_as_id_and_single_line_json():
    frame = stream.serialize_event({'run_id': 'r', 'sequence': 12, 'event_type': 'log',
                                    'at': 't', 'payload': {'line': '一\n二'}})
    lines = frame.rstrip('\n').split('\n')
    assert lines[0] == 'id: 12'
    assert lines[1] == 'event: ' + stream.SSE_EVENT_NAME
    assert lines[2].startswith('data: ') and lines[2].count('data: ') == 1
    assert json.loads(lines[2][len('data: '):])['payload']['line'] == '一\n二'
    assert stream.heartbeat_frame() == ': heartbeat\n\n'


def test_stream_replays_backlog_then_stops_at_terminal_event():
    run_id = _make_run()
    registry.append_event(run_id, 'run_started', {})
    registry.append_event(run_id, 'run_finished', {})

    async def collect():
        slept = []
        frames = []
        async def sleep(seconds):
            slept.append(seconds)
        async for frame in stream.event_stream(run_id, 0, sleep=sleep):
            frames.append(frame)
        return frames, slept

    frames, slept = asyncio.run(collect())
    assert [frame.split('\n')[0] for frame in frames] == ['id: 1', 'id: 2']
    assert slept == []  # 终态事件吐完就收流，不做多余轮询


def test_stream_resumes_from_cursor():
    run_id = _make_run()
    registry.append_event(run_id, 'run_started', {})
    registry.append_event(run_id, 'run_finished', {})

    async def collect():
        async def sleep(seconds):
            return None
        return [frame async for frame in stream.event_stream(run_id, 1, sleep=sleep)]

    frames = asyncio.run(collect())
    assert [frame.split('\n')[0] for frame in frames] == ['id: 2']


# --- HTTP 面 --------------------------------------------------------------

def test_meta_serves_backend_constants(client):
    body = client.get('/api/keyword-research/meta').json()
    assert body['marketplaces'][:5] == ['US', 'UK', 'DE', 'CA', 'FR']
    assert body['marketplaces'][5:] == sorted(MARKETPLACES.difference({'US','UK','DE','CA','FR'}))
    assert body['node_top_n_options'] == [50, 100]
    assert body['credential']['configured'] is False


def test_explicit_asin_and_node_modes_are_exclusive(client):
    asin='B0'+'SYN00001'
    asin_group={'marketplace':'US','mode':'asin','asins':[asin],
                'node_ids':[],'node_top_n':50}
    assert client.post('/api/keyword-research/plan',json={'groups':[asin_group]}).status_code==200

    node_group={'marketplace':'US','mode':'node','asins':[],
                'node_ids':['12345'],'node_top_n':100}
    assert client.post('/api/keyword-research/plan',json={'groups':[node_group]}).status_code==200

    invalid=[
        {**asin_group,'node_ids':['12345']},
        {**node_group,'asins':[asin]},
        {**node_group,'node_ids':['1','2']},
        {**node_group,'node_top_n':None},
    ]
    for group in invalid:
        response=client.post('/api/keyword-research/plan',json={'groups':[group]})
        assert response.status_code==400

    # Pre-mode snapshots retain the historical mixed-input interpretation.
    legacy={'marketplace':'US','asins':[asin],'node_ids':['12345'],'node_top_n':50}
    assert client.post('/api/keyword-research/plan',json={'groups':[legacy]}).status_code==200


def test_explicit_asin_mode_never_requests_node_ranking(tmp_path):
    from backend.keyword_research.pipeline import Pipeline

    class MCP:
        def product_node(self,*args):pytest.fail('ASIN mode must not validate a Node')
        def product_research(self,*args,**kwargs):pytest.fail('ASIN mode must not fetch a Node ranking')
    class Store:
        def log(self,*args):pass

    pipeline=Pipeline.__new__(Pipeline)
    pipeline.task_dir=tmp_path
    (tmp_path/'raw').mkdir()
    pipeline._cancel=threading.Event();pipeline.store=Store();pipeline.on_event=None
    pipeline.mcp=MCP()
    asin='B0'+'SYN00002'
    pool=asyncio.run(pipeline._step_nodes_and_pool({'groups':[{
        'marketplace':'US','mode':'asin','asins':[asin],'node_ids':[]}], 'options':{}}))
    assert [item['asin'] for item in pool]==[asin]


@pytest.mark.parametrize(('top_n', 'expected_pages'),[(50,1),(100,2)])
def test_node_ranking_fetches_pages_for_selected_top_n(tmp_path,top_n,expected_pages):
    from backend.keyword_research.pipeline import Pipeline

    class MCP:
        def __init__(self):self.pages=[]
        async def product_node(self,*args):return {'data':{'nodeIdPath':'canonical/path'}}
        async def product_research(self,marketplace,path,month,page):
            self.pages.append(page)
            start=(page-1)*50
            return {'data':{'items':[{'asin':f'item-{rank}','subBsrRank':rank}
                                     for rank in range(start+1,start+51)]}}
    class Store:
        def log(self,*args):pass

    pipeline=Pipeline.__new__(Pipeline)
    pipeline.task_dir=tmp_path
    (tmp_path/'raw').mkdir()
    pipeline._cancel=threading.Event();pipeline.store=Store();pipeline.on_event=None
    pipeline.mcp=MCP()
    result=asyncio.run(pipeline._step_nodes_and_pool({'groups':[{
        'marketplace':'US','mode':'node','asins':[],'node_ids':['node-1'],
        'node_top_n':top_n}], 'options':{}}))
    assert len(result)==top_n
    assert pipeline.mcp.pages==list(range(1,expected_pages+1))


def test_plan_reports_every_problem_at_once(client):
    response = client.post('/api/keyword-research/plan',
                           json={'groups': [{'marketplace': 'XX', 'asins': ['nope']}]})
    assert response.status_code == 400
    errors = response.json()['detail']['errors']
    assert len(errors) >= 3


def test_plan_hash_covers_the_matrix_switch(client):
    base = {'groups': [{'marketplace': 'US', 'asins': ASINS}], 'matrix_enabled': True}
    with_matrix = client.post('/api/keyword-research/plan', json=base).json()
    without = client.post('/api/keyword-research/plan',
                          json={**base, 'matrix_enabled': False}).json()
    assert with_matrix['plan_hash'] != without['plan_hash']


def test_run_creation_requires_matching_plan_hash(client):
    body = {'groups': [{'marketplace': 'US', 'asins': ASINS}], 'matrix_enabled': True}
    assert client.post('/api/keyword-research/runs',
                       json={**body, 'plan_hash': 'nope'}).status_code == 400


def test_unknown_run_is_404(client):
    assert client.get('/api/keyword-research/runs/' + 'a' * 32).status_code == 404
    assert client.get('/api/keyword-research/runs/' + 'a' * 32 + '/events').status_code == 404


def test_credentials_roundtrip_never_echoes_the_secret(client):
    saved = client.post('/api/keyword-research/credentials', json={'secret_key': 'sk-live-1'})
    assert saved.status_code == 200 and saved.json()['configured'] is True
    assert 'sk-live-1' not in saved.text
    assert client.get('/api/keyword-research/credentials').json()['configured'] is True
    cleared = client.delete('/api/keyword-research/credentials')
    assert cleared.json()['configured'] is False
    assert credentials.read_secret() == ''


def test_mcp_endpoint_requires_a_configured_secret():
    from backend.runtime_isolation import CredentialUnavailable
    with pytest.raises(CredentialUnavailable):
        credentials.mcp_endpoint()
    credentials.save_secret('sk-live-1')
    assert credentials.mcp_endpoint() == 'https://mcp.sellersprite.com/mcp'
    assert credentials.mcp_headers() == {'secret-key': 'sk-live-1'}


# --- runner ---------------------------------------------------------------

class _StubPipeline:
    """按 Pipeline 的接口行事：构造、run()、on_event、store.set_phase/log。"""

    def __init__(self, config, task_dir, store, on_event=None, cancel_event=None):
        self.config, self.task_dir, self.store, self.on_event = config, task_dir, store, on_event
        self.cancel_event = cancel_event

    def run(self):
        self.store.set_phase(self.task_dir, 'collecting', '开始数据采集')
        self.on_event({'kind': 'call_start', 'seq': 1, 'tool': 'traffic_extend', 'scope': 'US'})
        self.on_event({'kind': 'call_success', 'seq': 1, 'tool': 'traffic_extend',
                       'returned': 20, 'truncated': False})
        if self.cancel_event.is_set():
            self.store.set_phase(self.task_dir, 'canceled', '任务已取消')
            return {'status': 'canceled'}
        (self.task_dir / 'output').mkdir(exist_ok=True)
        (self.task_dir / 'output' / 'US_品类_asin_20260923.xlsx').write_bytes(b'report')
        self.store.set_phase(self.task_dir, 'done', '任务完成')
        return {'status': 'done'}


def _fake_provider_home(tmp_path):
    """用一个合成的 provider home 让凭据解析走完整路径；config.toml 用生产侧写入器落盘，
    格式不会与真实设置页漂移。"""
    from backend.model_settings import _write_config
    home = tmp_path / 'provider-home'
    home.mkdir(parents=True, exist_ok=True)
    _write_config(home / 'config.toml', {
        'default_model': 'fake/m1',
        'providers': [{'name': 'fake', 'kind': 'openai', 'base_url': 'https://fake.invalid',
                       'api_key_env': 'FAKE_KEY', 'models': ['m1']}]})
    (home / '.env').write_text('FAKE_KEY=sk-fake\n', encoding='utf-8')
    from backend.credential_vault import set_secret
    set_secret('model', 'FAKE_KEY', 'sk-fake')
    credentials.save_secret('sk-live-1')


def test_execute_records_stages_artifacts_and_terminal_event(monkeypatch, tmp_path):
    _fake_provider_home(tmp_path)
    monkeypatch.setattr(runner, 'Pipeline', _StubPipeline)
    run_id = _make_run()

    assert runner.execute(run_id, threading.Event())['status'] == registry.DONE
    state = registry.read(run_id)
    assert state['status'] == registry.DONE
    assert state['attempt'] == 1  # only a real execution increments attempts
    assert state['stage'] == 'done'
    assert state['model'] == {'base_url': 'https://fake.invalid', 'model': 'm1'}
    assert state['engine']['sha256'] and state['engine']['files'] > 0
    assert 'llm_api_key' not in json.dumps(state)

    artifacts = state['artifacts']
    assert len(artifacts) == 1
    assert artifacts[0]['name'].endswith('.xlsx')
    assert artifacts[0]['bytes'] == len(b'report')
    assert len(artifacts[0]['sha256']) == 64

    events = registry.read_events(run_id)
    assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
    types = [event['event_type'] for event in events]
    assert types[0] == 'run_started'
    assert types[-1] == 'run_finished'
    assert types.count('stage_started') == 2
    assert types.count('stage_finished') == 2
    assert 'artifact_ready' in types
    progress = [event for event in events if event['event_type'] == 'stage_progress']
    assert progress[0]['payload'] == {'stage': 'collecting', 'done': 1, 'total': None,
                                      'unit': 'mcp_call', 'message': '取数 1 完成: traffic_extend → 20 条'}


def test_execute_marks_failed_without_credentials(monkeypatch):
    monkeypatch.setattr(runner, 'Pipeline', _StubPipeline)
    run_id = _make_run()
    with pytest.raises(Exception):
        runner.execute(run_id, threading.Event())
    state = registry.read(run_id)
    assert state['status'] == registry.FAILED
    assert state['error']
    assert registry.read_events(run_id)[-1]['event_type'] == 'run_failed'


def test_execute_honours_a_preset_cancel(monkeypatch, tmp_path):
    _fake_provider_home(tmp_path)
    monkeypatch.setattr(runner, 'Pipeline', _StubPipeline)
    run_id = _make_run()
    preset = threading.Event()
    preset.set()

    assert runner.execute(run_id, preset)['status'] == registry.CANCELLED
    assert registry.read(run_id)['status'] == registry.CANCELLED
    assert registry.read(run_id)['artifacts'] == []
    assert registry.read_events(run_id)[-1]['event_type'] == 'run_cancelled'


def test_cancel_requires_this_process_to_own_the_run():
    run_id = _make_run()
    registry.write(run_id, status=registry.RUNNING)
    with pytest.raises(ValueError):
        runner.cancel(run_id)  # 本进程没有它的执行记录
    runner.RUNNING[run_id] = (threading.Event(), None)
    try:
        assert runner.cancel(run_id) == registry.CANCELLING
        assert registry.read(run_id)['status'] == registry.CANCELLING
    finally:
        runner.RUNNING.pop(run_id, None)


def test_routes_are_actually_mounted(client):
    """挂载检查走 HTTP：这个 FastAPI 版本把 include_router 包成 _IncludedRouter，
    所以直接翻 app.routes 看不到它们。"""
    assert client.get('/api/keyword-research/meta').status_code == 200
    assert client.get('/api/keyword-research/runs').status_code == 200


def test_report_template_is_generated_into_the_data_directory():
    """报告模板不入库、也不写程序目录：缺时生成到实例数据目录，并作为报告输入被登记哈希。"""
    from backend.db import STORAGE
    from backend.keyword_research import engine_runner

    path = engine_runner.template_path()
    assert path.is_file()
    assert STORAGE in path.parents
    assert not (engine_runner.ENGINE_ROOT / 'assets').exists()
    digest = engine_runner.template_digest()
    assert digest['name'] == 'keyword_market_report_template.xlsx'
    assert digest['bytes'] > 0 and len(digest['sha256']) == 64
    # 二次调用不再重新生成（同一份模板，哈希不变）
    assert engine_runner.template_digest() == digest


def test_engine_command_bootstraps_the_scripts_dir(tmp_path):
    """子进程必须自己把引擎脚本目录放进 sys.path。

    随包 runtime 是孤立型解释器（flags.safe_path=True、isolated=1，等价 -I）：它既不把「脚本
    所在目录」放进 sys.path，也忽略 PYTHON* 环境变量；开发 venv 两样都正常。所以同一个脚本在
    开发机上跑得通、打包后就 `ModuleNotFoundError: No module named 'classify_keywords'`——
    2026-09-23 的真实回合正是这样倒在 validate_labels.py 上。这里用 -I 模拟打包环境，正反两面实证。
    """
    import subprocess
    import sys

    from backend.keyword_research import engine_runner

    (tmp_path / 'engine_sibling_probe.py').write_text('VALUE = 7\n', encoding='utf-8')
    probe = tmp_path / 'probe.py'
    probe.write_text('from engine_sibling_probe import VALUE\nprint(VALUE)\n', encoding='utf-8')

    command = engine_runner.command_for(probe, (), scripts_dir=tmp_path)
    guarded = subprocess.run([command[0], '-I', *command[1:]], capture_output=True, text=True)
    assert guarded.returncode == 0, guarded.stderr
    assert guarded.stdout.strip() == '7'

    # 反证：不带引导、直接跑，-I 下兄弟导入必然失败
    without = subprocess.run([sys.executable, '-I', str(probe)], capture_output=True, text=True)
    assert without.returncode != 0
    assert 'ModuleNotFoundError' in without.stderr


@needs_fixtures
def test_runner_produces_a_downloadable_report_over_the_real_pipeline(monkeypatch, tmp_path, client):
    """工作台自己的那条路：运行登记 → runner → 真 Pipeline（录制夹具）→ 报告 → HTTP 下载。

    这是「页面发起就能拿到 Excel」在浏览器之外能拿到的最强证据：引擎不是桩，
    夹具是历史真实录制响应，产物经 HTTP 取回并与 run.json 里登记的哈希一致。
    """
    from backend.keyword_research.pipeline import Pipeline
    from tests.test_keyword_research_pipeline import (FIXTURES, HIST_ASINS, FakeLLM, FakeMCP)

    _fake_provider_home(tmp_path)

    def factory(config, task_dir, store, on_event=None, cancel_event=None):
        pipeline = Pipeline(config, task_dir, store, on_event=on_event,
                            cancel_event=cancel_event, llm=FakeLLM())
        pipeline.mcp = FakeMCP(FIXTURES, pipeline.rate_limiter, pipeline.call_log,
                              on_event=on_event)
        return pipeline

    monkeypatch.setattr(runner, 'Pipeline', factory)
    monkeypatch.setattr(runner.credentials, 'model_target',
                        lambda *a, **k: ('https://fake.invalid', 'sk-fake', 'm1'))
    monkeypatch.setattr(runner.credentials, 'mcp_endpoint', lambda: 'https://fake.invalid/mcp')
    # 限流放开：真实等待只由 rate_limiter 自己的测试覆盖，否则这里要空等三分钟
    from backend.keyword_research.config import EngineConfig
    monkeypatch.setattr(runner, 'EngineConfig',
                        lambda **kw: EngineConfig(**{**kw, 'window_max_calls': 1000,
                                                     'min_call_interval': 0.0}))
    run_id = registry.create({
        'groups': [{'marketplace': 'US', 'asins': HIST_ASINS, 'node_ids': [], 'node_top_n': 50}],
        'options': {'data_month': '202606', 'keyword_pool_pages': 3, 'trend_months': 24,
                    'core_trend_keywords': 5, 'longtail_trend_keywords': 5,
                    'trend_keyword_cap': 20, 'current_ppc': True, 'historical_ppc': False,
                    'report_language': 'zh-CN', 'matrix_enabled': True}},
        {'confirmed': True, 'planned_total': 32, 'hard_cap': 35})

    result = runner.execute(run_id, threading.Event())

    assert result['status'] == registry.DONE
    name = result['artifacts'][0]['name']
    assert name.endswith('.xlsx')

    import openpyxl
    downloaded = client.get(f'/api/keyword-research/runs/{run_id}/artifacts/{name}')
    assert downloaded.status_code == 200
    assert len(downloaded.content) == result['artifacts'][0]['bytes']
    path = registry.run_dir(run_id) / 'output' / name
    assert downloaded.content == path.read_bytes()
    assert openpyxl.load_workbook(path).sheetnames[0] == '00_市场看板'

    events = registry.read_events(run_id, limit=1000)
    assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]['event_type'] == 'run_finished'
    assert 'artifact_ready' in [event['event_type'] for event in events]
