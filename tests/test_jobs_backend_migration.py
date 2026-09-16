"""Background jobs build their runtimes through the backend contract, not the legacy client.

The per-product worker and the text-facts coordinator were the last production callers of the
legacy ``harness.ACP`` client. These tests drive both real execution paths (``jobs._execute`` and
``jobs._chat``) and assert on what the runtime is actually told: which runtime directory it runs
in, which identity reaches its tool environment, which tools it can see, and how failures and
cancellations reach the existing job state.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend import agent_application, harness, jobs, runtime_isolation, service
from backend.agent import events as event_store, reasonix
from backend.agent.backend import RuntimeUnavailable, TurnError
from backend.db import Dataset, Message, Run, Session, STORAGE, Tag, require, uid

DEFS=[{'key':'color','name':'颜色','type':'FACT','definition':'仅提取标题或 SKU 中明确声明的颜色，不确定则留空','options':[],'source':'text'}]
STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
# The wire ids the deterministic peer asks with, per tests/fixtures/stub_acp_cli.py.
WIRE_QUESTION_ID=900
WIRE_PERMISSION_ID=901
QUESTION_TOOL='mcp__product_workbench__ask_business_question'


def open_run(ds):
    sc=service.propose(ds['id'],'流程测试',DEFS);service.approve_schema(sc['id'])
    return service.create_run(ds['id'],sc['id'],'sample','test')


def start_turn(did):
    """The assistant row a chat turn writes into, exactly as jobs.start_chat creates it."""
    with Session.begin() as s:
        m=Message(id=uid(),dataset_id=did,role='assistant',text='',status='running')
        s.add(m);s.flush();return m.id


def runtime_toml(adapter):
    """The runtime directory config the backend was launched with, as it was at launch.

    One runtime directory is shared by every product of a run, so the file is captured when the
    backend is built instead of being read back after the run has rewritten it.
    """
    return adapter.toml


def run_worker(run_id):
    """Drive the worker path exactly as jobs.launch_run does before its thread starts."""
    jobs.active[run_id]={'stop':False,'client':None}
    jobs._execute(run_id)


def tool_env(adapter,key):
    """One value the runtime directory hands to the tool server of that runtime."""
    found=re.search(r'"%s"\s*=\s*"([^"]*)"'%key,runtime_toml(adapter))
    return found.group(1) if found else None


def toml_list(adapter,key):
    found=re.search(r'^%s = (\[.*\])$'%key,runtime_toml(adapter),re.M)
    return json.loads(found.group(1)) if found else []


@pytest.fixture(autouse=True)
def clean_job_state():
    """jobs keeps its running work in module state, so one test must not inherit another's."""
    jobs.active.clear();jobs.chats.clear()
    yield
    jobs.active.clear();jobs.chats.clear()


@pytest.fixture
def backends(monkeypatch):
    """Every backend a job builds, plus the hooks that script one of them.

    The backend itself is replaced, so the test can see the launch facts the job chose and make
    the runtime behave — but everything above it is the real code path.
    """
    built=[];state={'script':[{'type':'assistant_text_delta','text':'已读取'},
                              {'type':'tool_call_started','tool_call_id':'c1','tool':'read_worksheet','host_status':'pending'},
                              {'type':'tool_call_settled','tool_call_id':'c1','tool':'read_worksheet','host_status':'completed'}]}
    class FakeAdapter:
        def __init__(self,config):
            self.config=config;self.sink=None;self.sent=[];self.cancelled=False;self.stopped=False
            self.toml=(Path(config.cwd)/'reasonix.toml').read_text(encoding='utf-8')
            built.append(self)
        def bind(self,sink):self.sink=sink
        def start(self):pass
        def send(self,text):
            self.sent.append(text)
            for event in state['script']:self.sink(dict(event))
            hook=state.get('on_send')
            if hook:hook(self,text)
        def cancel(self):
            self.cancelled=True
            hook=state.get('on_cancel')
            if hook:hook(self)
            return True
        def stop(self):self.stopped=True
        def status(self):return {'provider':'reasonix','model':self.config.model,'alive':True}
    monkeypatch.setattr(reasonix,'ReasonixAdapter',FakeAdapter)
    monkeypatch.setattr(runtime_isolation,'environment',lambda *a,**k:{})
    monkeypatch.setattr(runtime_isolation,'credential_selection',lambda *a,**k:('test','cred-1'))
    return built,state


def poison_legacy_client(monkeypatch):
    """Put a recorder where the retired client used to live.

    ``harness.ACP`` no longer exists, so the injection has to create the attribute. That is the
    point: a composition that reaches for it again is recorded here and fails the test, whether it
    reaches through ``jobs`` or through the agent application.
    """
    built=[]
    class PoisonACP:
        def __init__(self,*args,**kwargs):
            built.append(args)
            raise AssertionError('production composition constructed the retired harness.ACP')
    monkeypatch.setattr(harness,'ACP',PoisonACP,raising=False)
    return built


def test_the_retired_client_is_not_defined_by_the_launch_module():
    assert not hasattr(harness,'ACP'),'harness must not define a runtime client any more'


def test_T1_the_worker_runs_on_the_backend_contract_not_the_legacy_client(dataset,backends,monkeypatch):
    built,state=backends
    legacy=poison_legacy_client(monkeypatch)
    run=open_run(dataset)
    run_worker(run['id'])
    assert legacy==[]                       # the legacy client is never constructed
    assert not hasattr(jobs,'ACP')
    assert len(built)==len(run['product_ids'])>0
    assert all(x.config.runtime_key.startswith('job:') for x in built)
    assert all(x.sent and x.sent[0].startswith('执行单商品打标') for x in built)
    assert all(x.stopped for x in built)    # every worker runtime is released


def test_T2_the_coordinator_runs_on_the_backend_contract(dataset,backends,monkeypatch):
    built,state=backends
    legacy=poison_legacy_client(monkeypatch)
    did=dataset['id'];ident=start_turn(did)
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'看图片','test/model')
    assert legacy==[]
    assert len(built)==1
    assert built[0].sent[0].endswith('本轮用户要求：看图片')
    assert built[0].stopped
    with Session() as s:
        m=require(s,Message,ident)
        assert m.status=='completed' and m.text=='已读取'
        assert m.events==[{'type':'tool','toolCallId':'c1','title':'read_worksheet','status':'pending'},
                          {'type':'tool','toolCallId':'c1','title':'read_worksheet','status':'completed'}]
    assert jobs.chats=={}


def test_the_agent_ui_composition_never_reaches_the_retired_client(backends,monkeypatch):
    """The UI agent is the other production composition; it must build through the same contract.

    The application binds the adapter in its own module namespace, so the recorder is patched there
    as well — the point is which *launch facts* the composition produces, not which class it names.
    """
    built,state=backends
    legacy=poison_legacy_client(monkeypatch)
    made=[]
    class Recorder:
        def __init__(self,config):self.config=config;made.append(self)
    monkeypatch.setattr(agent_application,'ReasonixAdapter',Recorder)
    adapter=agent_application.backend_factory('ds-agent','test/model')()
    assert legacy==[]
    assert len(made)==1
    assert Path(adapter.config.cwd)==Path(STORAGE)/'harness'/'ds-agent'/'agent'
    assert adapter.config.runtime_key=='agent:ds-agent'
    assert adapter.config.model=='test/model'


def test_T3_the_worker_runtime_carries_the_trusted_identity(dataset,backends):
    built,state=backends
    run=open_run(dataset)
    run_worker(run['id'])
    assert {tool_env(x,'TAG_PRODUCT_ID') for x in built}==set(run['product_ids'])
    for adapter in built:
        assert Path(adapter.config.cwd)==Path(STORAGE)/'harness'/dataset['id']/run['id']
        assert tool_env(adapter,'TAG_DATASET_ID')==dataset['id']
        assert tool_env(adapter,'TAG_RUN_ID')==run['id']
        # identity is injected, never asserted to the model in the prompt
        assert tool_env(adapter,'TAG_PRODUCT_ID') not in adapter.sent[0] and run['id'] not in adapter.sent[0]


def test_T3_the_coordinator_runtime_carries_no_run_identity(dataset,backends):
    built,state=backends
    did=dataset['id'];ident=start_turn(did)
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'你好','test/model')
    assert tool_env(built[0],'TAG_DATASET_ID')==did
    assert tool_env(built[0],'TAG_RUN_ID') is None and tool_env(built[0],'TAG_PRODUCT_ID') is None


def test_T4_worker_tool_side_effects_land_in_the_existing_result_tables(dataset,backends):
    built,state=backends
    run=open_run(dataset)
    def record(adapter,text):
        service.record_tags(run['id'],tool_env(adapter,'TAG_PRODUCT_ID'),
            [{'key':'color','raw_value':'Blue','normalized_value':'蓝色','confidence':.9,'source':'text','evidence':'夹具：SKU Color: Blue','asset_ids':[]}])
    state['on_send']=record
    run_worker(run['id'])
    with Session() as s:
        r=require(s,Run,run['id'])
        assert r.status=='completed' and r.errors=={}
        assert s.scalar(select(func.count()).select_from(Tag).where(Tag.run_id==run['id']))==len(run['product_ids'])
        assert require(s,Dataset,dataset['id']).stage=='CALIBRATION'


def test_T6_cancelling_a_run_reaches_the_backend_and_leaves_nothing_active(dataset,backends):
    built,state=backends
    run=open_run(dataset)
    state['on_send']=lambda adapter,text:jobs.stop_run(run['id'])
    run_worker(run['id'])
    assert built[0].cancelled                      # the job's stop reaches the backend
    assert len(built)==1                           # and is honoured before the next product
    assert run['id'] not in jobs.active
    assert built[0].stopped
    with Session() as s:assert require(s,Run,run['id']).status=='paused'


def test_T7_backend_failures_land_in_the_existing_run_error_state(dataset,backends):
    built,state=backends
    run=open_run(dataset)
    for error in (TurnError('回合执行失败：网络不可达'),RuntimeUnavailable('运行时启动失败')):
        with Session.begin() as s:require(s,Run,run['id']).status='running'
        with Session.begin() as s:require(s,Run,run['id']).errors={}
        built.clear()
        def fail(adapter,text,error=error):raise error
        state['on_send']=fail
        run_worker(run['id'])
        with Session() as s:
            r=require(s,Run,run['id'])
            assert r.status=='partial'
            assert set(r.errors.values())=={str(error)}   # the backend's own words, unclassified
        assert run['id'] not in jobs.active


def test_T8_no_background_runtime_can_see_the_business_question_tool(dataset,backends):
    built,state=backends
    run=open_run(dataset)
    run_worker(run['id'])
    did=dataset['id'];ident=start_turn(did)
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'你好','test/model')
    for adapter in built:
        assert 'ask_business_question' not in runtime_toml(adapter)
        assert 'TAG_QUESTION_OWNER' not in runtime_toml(adapter)
        assert QUESTION_TOOL not in toml_list(adapter,'allow')


def test_T9_background_jobs_open_no_agent_session_or_event_log(dataset,backends,monkeypatch):
    built,state=backends
    touched=[]
    for name in ('open_session','append','update_session','close_session','persisting_consumer'):
        monkeypatch.setattr(event_store,name,lambda *a,_n=name,**k:touched.append(_n))
    run=open_run(dataset)
    run_worker(run['id'])
    did=dataset['id'];ident=start_turn(did)
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'你好','test/model')
    assert touched==[]
    assert event_store.list_sessions(dataset_id=did)==[]


# --- the refusal path, over a deterministic peer ---------------------------
#
# The adapter announces an inbound interaction and waits for the host; the bridge is what normally
# answers it. A background job has no bridge and nobody to ask, so its own sink must refuse. That
# contract is invisible to the tests above, which replace the backend before any of this happens, so
# it is driven here through the real composition and a real (deterministic) peer.

def frames(log):
    return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]


def replies(log,wire_id):
    """What the client sent back for the peer's own request id, still carrying that same id."""
    return [frame for frame in frames(log) if frame.get('id')==wire_id and 'method' not in frame]


def no_agent_surface(monkeypatch):
    """Nothing UI-shaped may exist for a background runtime: no bridge, no session, no event log."""
    touched=[];bridges=[]
    for name in ('open_session','append','update_session','close_session'):
        monkeypatch.setattr(event_store,name,lambda *a,_n=name,**k:touched.append(_n))
    monkeypatch.setattr(agent_application,'AgentBridge',lambda *a,**k:bridges.append(a))
    return touched,bridges


def assert_refused(touched,bridges,log,wire_id,did):
    assert [frame['result'] for frame in replies(log,wire_id)]==[{'outcome':{'outcome':'cancelled'}}]
    assert touched==[] and bridges==[]
    assert event_store.list_sessions(dataset_id=did)==[]


@pytest.fixture
def peer(tmp_path,monkeypatch):
    """Point the real jobs composition at the deterministic ACP peer.

    jobs still chooses the runtime directory and builds the real adapter and runtime; only the
    executable is replaced, with the peer placed in the working directory the adapter's own launch
    command expects to find it in. The prompt budget is shortened so a composition that waits fails
    in seconds instead of at the production bound.
    """
    state={'mode':'normal'};log=tmp_path/'frames.jsonl'
    directory=jobs.runtime_dir
    def runtime_dir(*args,**kwargs):
        path=directory(*args,**kwargs)
        shutil.copy(STUB,Path(path)/'acp')
        return path
    monkeypatch.setattr(jobs,'runtime_dir',runtime_dir)
    monkeypatch.setattr(jobs,'executable',lambda:sys.executable)
    monkeypatch.setattr(runtime_isolation,'environment',
        lambda did,session_id=None,credential_ids=None:dict(os.environ,STUB_MODE=state['mode'],STUB_LOG=str(log)))
    monkeypatch.setattr(runtime_isolation,'credential_selection',lambda *a,**k:('test','cred-1'))
    monkeypatch.setattr(reasonix,'PROMPT_TIMEOUT_SECONDS',6.0)
    return state,log


@pytest.fixture
def live_adapters():
    opened=[]
    yield opened
    for adapter in opened:
        try:adapter.stop()
        except Exception:pass


def test_the_worker_composition_refuses_a_question_instead_of_waiting(dataset,peer,live_adapters,monkeypatch):
    state,log=peer
    state['mode']='ask_client'
    did=dataset['id']
    touched,bridges=no_agent_surface(monkeypatch)
    client=jobs._backend(did,'test/model','run-refusal','product-refusal',role='worker')
    live_adapters.append(client)
    client.send('执行单商品打标。')          # returns only because the request was answered at once
    client.stop()
    assert client.status()['alive'] is False
    assert client.diagnostics()['pending_requests']==[]
    assert_refused(touched,bridges,log,WIRE_QUESTION_ID,did)


def test_the_coordinator_composition_refuses_a_permission_instead_of_waiting(dataset,peer,monkeypatch):
    state,log=peer
    state['mode']='permission_client'
    did=dataset['id'];ident=start_turn(did)
    touched,bridges=no_agent_surface(monkeypatch)
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'看图片','test/model')      # completes only because the request was answered
    with Session() as s:
        m=require(s,Message,ident)
        assert m.status=='completed'
        assert all(event.get('type')!='permission_denied' for event in (m.events or []))
    assert_refused(touched,bridges,log,WIRE_PERMISSION_ID,did)
