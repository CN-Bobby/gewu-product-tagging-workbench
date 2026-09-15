"""M3C: the agent HTTP surface driving the real AgentBridge and the real event store.

The only thing replaced here is the backend factory, which is the single seam the application
exposes for exactly this purpose. Everything else is real: the same bridge production uses, the
same event rows, the same FastAPI app. So what these tests pin is that HTTP is a faithful command
channel — no second event channel, no session lifecycle invented at the HTTP layer, and no runtime
detail escaping into a response.
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import agent_application
from backend.agent import events as store
from backend.agent.backend import TurnError
from backend.app import app
from fake_backend import FakeBackend

DATASET='ds-m3c'
STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
MODEL='deepseek/deepseek-v4-flash'


@pytest.fixture
def client():
    with TestClient(app) as test_client:yield test_client


@pytest.fixture
def workspace(tmp_path):
    """A working directory holding the deterministic ACP peer, for the one test that needs a real
    teardown under a real turn instead of a double."""
    cwd=tmp_path/'cwd';cwd.mkdir()
    (cwd/'PRODUCT_ANALYST_AGENT.md').write_text('stub prompt\n',encoding='utf-8')
    (cwd/'reasonix.toml').write_text('[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\n',encoding='utf-8')
    shutil.copy(STUB,cwd/'acp')
    return cwd


@pytest.fixture
def adapters():
    opened=[]
    yield opened
    for adapter in opened:
        try:adapter.stop()
        except Exception:pass


def peer_methods(request_log):
    if not request_log.exists():return []
    return [json.loads(line).get('method') for line in
            request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def install(monkeypatch,mode='normal',**kwargs):
    """Replace the one seam: a bridge that drives a deterministic double, everything else real."""
    created=[]
    def factory(dataset_id,model):
        def build():
            backend=FakeBackend(mode=mode,**kwargs)
            created.append(backend)
            return backend
        return build
    monkeypatch.setattr(agent_application,'backend_factory',factory)
    monkeypatch.setattr(agent_application,'_BRIDGES',{})
    monkeypatch.setattr(agent_application,'_SESSIONS',{})
    return created


def new_session(client):
    response=client.post('/api/agent/sessions',json={'dataset_id':DATASET,'model':MODEL})
    assert response.status_code==200,response.text
    session_id=response.json()['id']
    assert client.post('/api/agent/sessions/%s/start'%session_id).status_code==200
    return session_id


def kinds(session_id):
    return [row['event_type'] for row in store.read(session_id,after_sequence=0,limit=1000)]


def start_message(client,session_id,text='你好'):
    """POST a message on its own thread so a cancel or stop can race it."""
    outcome={}
    def drive():outcome['response']=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':text})
    worker=threading.Thread(target=drive,daemon=True);worker.start()
    outcome['worker']=worker
    return outcome


def wait_for(produce,timeout=10):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=produce()
        if value:return value
        time.sleep(0.02)
    return None


# --- the command channel ---------------------------------------------------

def test_a_message_runs_a_turn_through_the_bridge(client,monkeypatch):
    created=install(monkeypatch,'normal')
    session_id=new_session(client)
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'你好'})
    assert response.status_code==200,response.text
    body=response.json()
    assert body['session_id']==session_id and body['outcome']=='completed'
    assert body['message_id'] and body['sequence']
    # the turn really went through the bridge into the backend, and its events are in the store
    assert created[0].sent==['你好']
    assert created[0].started is True
    order=kinds(session_id)
    assert 'message_completed' in order
    assert order.index('message_started')<order.index('turn_settled')<order.index('message_completed')


def test_the_response_carries_no_output_because_the_store_does(client,monkeypatch):
    """One event channel: the reply text is in the event store, not in the command response."""
    install(monkeypatch,'normal')
    session_id=new_session(client)
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'你好'})
    assert '你好' not in json.dumps(response.json(),ensure_ascii=False)
    deltas=[row['payload']['text'] for row in store.read(session_id,0,1000)
            if row['event_type']=='assistant_text_delta']
    assert ''.join(deltas)=='你好'


def test_status_reports_the_session(client,monkeypatch):
    install(monkeypatch,'normal')
    session_id=new_session(client)
    body=client.get('/api/agent/sessions/%s'%session_id).json()
    assert body['session_id']==session_id and body['status']=='active'
    assert body['remote_session_id']=='fake-remote-1'
    assert body['backend']['started'] is True


def test_an_empty_message_is_refused(client,monkeypatch):
    install(monkeypatch,'normal')
    session_id=new_session(client)
    for body in ({'text':''},{'text':'   '},{}):
        assert client.post('/api/agent/sessions/%s/messages'%session_id,json=body).status_code in (400,422)


# --- application errors ----------------------------------------------------

def test_a_turn_error_is_reported_without_failing_the_session(client,monkeypatch):
    created=install(monkeypatch,'turn_fail')
    session_id=new_session(client)
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'坏的'})
    assert response.status_code==502
    error=response.json()['detail']
    assert error['code']=='turn_error' and error['fatal'] is False
    assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='active'
    created[0].mode='normal'
    assert client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'好的'}).status_code==200


def test_a_cancelled_turn_leaves_the_session_usable(client,monkeypatch):
    created=install(monkeypatch,'slow')
    session_id=new_session(client)
    message=start_message(client,session_id,'慢慢说')
    assert wait_for(lambda:created[0].entered.is_set() or None)
    cancel=client.post('/api/agent/sessions/%s/cancel'%session_id)
    assert cancel.status_code==200 and cancel.json()['cancel_requested'] is True
    message['worker'].join(timeout=15)
    assert not message['worker'].is_alive()
    assert message['response'].status_code==409
    assert message['response'].json()['detail']['code']=='cancelled'
    assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='active'
    created[0].mode='normal'
    assert client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'再来'}).status_code==200


def test_a_fatal_backend_error_fails_the_session(client,monkeypatch):
    install(monkeypatch,'fatal_fail')
    session_id=new_session(client)
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'会崩'})
    assert response.status_code==503
    error=response.json()['detail']
    assert error['code']=='runtime_unavailable' and error['fatal'] is True
    assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='failed'
    assert client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'之后'}).status_code==409


def test_stop_ends_the_session_and_later_commands_are_refused(client,monkeypatch):
    install(monkeypatch,'normal')
    session_id=new_session(client)
    stopped=client.post('/api/agent/sessions/%s/stop'%session_id)
    assert stopped.status_code==200 and stopped.json()['status']=='stopped'
    assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='stopped'
    assert client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'之后'}).status_code==409
    assert kinds(session_id)[-2:]==['session_stopping','session_stopped']


def test_an_idle_cancel_reports_that_nothing_was_requested(client,monkeypatch):
    install(monkeypatch,'normal')
    session_id=new_session(client)
    body=client.post('/api/agent/sessions/%s/cancel'%session_id).json()
    assert body['cancel_requested'] is False
    assert 'message_cancel_requested' not in kinds(session_id)


def test_an_unknown_session_is_handled_for_every_operation(client,monkeypatch):
    install(monkeypatch,'normal')
    missing='/api/agent/sessions/no-such-session'
    assert client.get(missing).status_code==404
    assert client.post(missing+'/start').status_code==404
    assert client.post(missing+'/messages',json={'text':'hi'}).status_code==404
    assert client.post(missing+'/cancel').status_code==404
    assert client.post(missing+'/stop').status_code==404
    for response in (client.get(missing),client.post(missing+'/messages',json={'text':'hi'})):
        assert response.json()['detail']['code']=='session_not_found'


def test_a_session_without_a_dataset_is_refused(client,monkeypatch):
    install(monkeypatch,'normal')
    assert client.post('/api/agent/sessions',json={}).status_code==400


# --- concurrency -----------------------------------------------------------

def test_message_and_cancel_racing_stay_bounded(client,monkeypatch):
    created=install(monkeypatch,'slow')
    session_id=new_session(client)
    for _ in range(3):
        message=start_message(client,session_id,'慢慢说')
        assert wait_for(lambda:created[-1].entered.is_set() or None)
        assert client.post('/api/agent/sessions/%s/cancel'%session_id).status_code==200
        message['worker'].join(timeout=15)
        assert not message['worker'].is_alive()
        assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='active'


def test_message_and_stop_racing_stay_bounded(client,monkeypatch,workspace,adapters):
    """A real teardown under a real turn, driven only through HTTP: both requests are bounded and
    the session still ends stopped rather than failed."""
    def factory(dataset_id,model):
        from backend.agent import reasonix
        def build():
            adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(
                executable=sys.executable,model=MODEL,cwd=str(workspace),
                env=dict(os.environ,STUB_MODE='ignore_cancel',STUB_LOG=str(workspace/'requests.jsonl')),
                runtime_key='m3c'))
            adapters.append(adapter)
            return adapter
        return build
    monkeypatch.setattr(agent_application,'backend_factory',factory)
    monkeypatch.setattr(agent_application,'_BRIDGES',{})
    monkeypatch.setattr(agent_application,'_SESSIONS',{})
    session_id=new_session(client)
    message=start_message(client,session_id,'很久的一轮')
    request_log=workspace/'requests.jsonl'
    assert wait_for(lambda:'session/prompt' in peer_methods(request_log))
    stopped=client.post('/api/agent/sessions/%s/stop'%session_id)
    message['worker'].join(timeout=20)
    assert stopped.status_code==200 and stopped.json()['status']=='stopped'
    assert not message['worker'].is_alive()
    assert message['response'].status_code==409
    assert client.get('/api/agent/sessions/%s'%session_id).json()['status']=='stopped'
    assert 'session_failed' not in kinds(session_id)
    assert 'session/close' in peer_methods(request_log)


# --- boundary --------------------------------------------------------------

def test_the_http_layer_knows_nothing_about_the_runtime():
    source=(Path(__file__).resolve().parents[1]/'backend'/'agent_http.py').read_text(encoding='utf-8')
    for token in ('reasonix','Reasonix','ACP','jsonrpc','session/prompt','session/cancel','session/close',
                  'stopReason','toolCallId','rawInput','mcp-tool:','wire_request_id','diagnostics',
                  'agent.runtime','Runtime(','subprocess'):
        assert token not in source,token
    imports=[line for line in source.splitlines() if line.startswith(('from ','import '))]
    assert not [line for line in imports if 'reasonix' in line or 'runtime' in line],imports


def test_a_response_never_carries_the_runtimes_own_error_text(client,monkeypatch):
    """The caller gets a stable code, a safe message and fatality — never the underlying text,
    which is where wire frames, command lines and filesystem paths live."""
    class LeakyBackend(FakeBackend):
        def send(self,text):
            raise TurnError('回合执行失败',detail='Reasonix session/prompt 超时 C:\\secret\\workbench.db')

    def factory(dataset_id,model):
        return lambda:LeakyBackend()
    monkeypatch.setattr(agent_application,'backend_factory',factory)
    monkeypatch.setattr(agent_application,'_BRIDGES',{})
    monkeypatch.setattr(agent_application,'_SESSIONS',{})
    session_id=new_session(client)
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'x'})
    assert response.status_code==502
    raw=response.text
    for leaked in ('Reasonix','session/prompt','secret','workbench.db','C:\\'):
        assert leaked not in raw,leaked
    assert response.json()['detail']['code']=='turn_error'
