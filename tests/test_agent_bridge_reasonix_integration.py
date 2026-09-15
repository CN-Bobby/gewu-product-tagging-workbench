"""M3B-7 layer A: the real AgentBridge driving the real ReasonixAdapter over a stub ACP peer.

Everything here goes through the public application surface — AgentBridge, AgentEventStore — and
the only thing underneath is the real adapter talking to a deterministic peer. The bridge is never
told which backend it owns: it receives one from a factory and drives it through the contract, so a
test that had to reach past the contract would be the failure this file exists to catch.

Nothing in this file may import the adapter by name except to construct it in a factory, and no
assertion may depend on a wire detail: the peer's JSON-RPC request id in particular must never
reach an event payload.
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.agent import events as store
from backend.agent import reasonix
from backend.agent.backend import CancelledError,RuntimeUnavailable,TurnError
from backend.agent.bridge import AgentBridge,InvalidSessionState

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='m3b7-runtime-key'
WIRE_QUESTION_ID=900
WIRE_PERMISSION_ID=901
DATASET='ds-m3b7'


@pytest.fixture
def workspace(tmp_path):
    cwd=tmp_path/'cwd';cwd.mkdir()
    (cwd/'PRODUCT_ANALYST_AGENT.md').write_text('stub prompt\n',encoding='utf-8')
    (cwd/'reasonix.toml').write_text('[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\n',encoding='utf-8')
    shutil.copy(STUB,cwd/'acp')
    return cwd


@pytest.fixture
def request_log(tmp_path):
    return tmp_path/'requests.jsonl'


@pytest.fixture
def adapters():
    opened=[]
    yield opened
    for adapter in opened:
        try:adapter.stop()
        except Exception:pass


def make_bridge(workspace,request_log,mode,opened=None):
    """The bridge gets a factory, never the adapter: that is the whole point of this layer."""
    def factory():
        adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(
            executable=sys.executable,model=MODEL,cwd=str(workspace),
            env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log)),runtime_key=RUNTIME_KEY))
        if opened is not None:opened.append(adapter)
        return adapter
    return AgentBridge(factory,dataset_id=DATASET)


def started(workspace,request_log,mode,opened=None):
    bridge=make_bridge(workspace,request_log,mode,opened)
    session_id=bridge.create_session(provider='reasonix',model=MODEL)['id']
    bridge.start(session_id)
    return bridge,session_id


def rows(session_id):
    return store.read(session_id,after_sequence=0,limit=1000)


def kinds(session_id):
    return [row['event_type'] for row in rows(session_id)]


def payloads(session_id,event_type):
    return [row['payload'] for row in rows(session_id) if row['event_type']==event_type]


def scalars(value,found=None):
    """Every scalar anywhere in a payload, so a leaked identifier cannot hide in a nested field."""
    found=[] if found is None else found
    if isinstance(value,dict):
        for key,item in value.items():found.append(key);scalars(item,found)
    elif isinstance(value,list):
        for item in value:scalars(item,found)
    else:found.append(value)
    return found


def wait_for(produce,timeout=15):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=produce()
        if value:return value
        time.sleep(0.02)
    return None


def sent_methods(request_log):
    if not request_log.exists():return []
    return [json.loads(line).get('method') for line in
            request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def turn_is_running(request_log,method='session/prompt',timeout=15):
    """A stored delta cannot prove this: the bridge buffers a short reply until something closes
    the turn. The peer's own request log is the honest signal that the turn really started."""
    return bool(wait_for(lambda:method in sent_methods(request_log),timeout))


def start_turn(bridge,session_id,text):
    outcome={}
    def drive():
        try:outcome['result']=bridge.send(session_id,text)
        except Exception as error:outcome['error']=error
    worker=threading.Thread(target=drive,daemon=True);worker.start()
    return outcome,worker


def first_interaction(session_id,event_type,timeout=15):
    return wait_for(lambda:payloads(session_id,event_type),timeout)


# --- contract 2 + 3: a plain text turn reaches the store in order ----------

def test_a_text_turn_reaches_the_event_store_in_order(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'prompt_multi',adapters)
    bridge.send(session_id,'说点什么')
    assert kinds(session_id)==[
        'session_created','session_starting','session_started',
        'message_started','assistant_text_delta','turn_settled','message_completed']
    # the coalesced text is exactly what the backend produced, in order
    assert ''.join(item['text'] for item in payloads(session_id,'assistant_text_delta'))=='你好M3B3_OK'
    assert payloads(session_id,'turn_settled')[0]['stop_reason']=='end_turn'
    assert bridge.get_session(session_id)['status']=='active'
    assert bridge.get_session(session_id)['remote_session_id']=='stub-session'


def test_sequences_are_dense_monotonic_and_never_repeat(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'prompt_multi',adapters)
    bridge.send(session_id,'第一轮')
    bridge.send(session_id,'第二轮')
    bridge.send(session_id,'第三轮')
    sequences=[row['sequence'] for row in rows(session_id)]
    assert sequences==list(range(1,len(sequences)+1))
    assert kinds(session_id).count('turn_settled')==3
    assert kinds(session_id).count('session_started')==1
    # every row belongs to this session and nothing is stored twice
    assert len({row['id'] for row in rows(session_id)})==len(sequences)
    assert all(row['session_id']==session_id for row in rows(session_id))


# --- contract 2: a tool turn keeps tool settlement before the terminal ------

def test_a_tool_turn_settles_before_the_turn_terminal(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'normal',adapters)
    bridge.send(session_id,'看下表')
    order=kinds(session_id)
    assert order.index('tool_call_started')<order.index('tool_call_settled')<order.index('turn_settled')
    started_payload=payloads(session_id,'tool_call_started')[0]
    settled_payload=payloads(session_id,'tool_call_settled')[0]
    assert started_payload['tool_call_id']==settled_payload['tool_call_id']
    assert (settled_payload['server'],settled_payload['tool'])==('product_workbench','read_worksheet')
    assert settled_payload['capability_id']=='mcp-tool:product_workbench/read_worksheet'
    assert settled_payload['host_status']=='completed'
    assert [row['source'] for row in rows(session_id) if row['event_type']=='tool_call_settled']==['tool']


# --- contract 2 + 11: an interaction is answered through the bridge --------

def widen_settlement_race(monkeypatch):
    """Answering wakes the peer, whose response can reach the turn terminal thread before the
    answering thread is done. The pause only widens that window so the ordering invariant is
    genuinely exercised — the adapter's interaction lock is what has to make it impossible, and
    this is the integration-level version of the same guard the adapter tests use."""
    original=reasonix.ReasonixAdapter._reply
    def reply_then_pause(self,wire_request_id,result):
        answered=original(self,wire_request_id,result)
        time.sleep(0.2)
        return answered
    monkeypatch.setattr(reasonix.ReasonixAdapter,'_reply',reply_then_pause)


def test_a_question_is_answered_through_the_bridge(workspace,request_log,adapters,monkeypatch):
    widen_settlement_race(monkeypatch)
    bridge,session_id=started(workspace,request_log,'ask_client',adapters)
    outcome,worker=start_turn(bridge,session_id,'帮我选一个格式')
    announced=first_interaction(session_id,'question_requested')
    assert announced, 'the question never reached the event store'
    question=announced[0]
    assert [option['option_id'] for option in question['options']]==['q1:1','q1:2','q1:cancel']
    assert bridge.answer_question(session_id,question['interaction_id'],'q1:2') is True
    worker.join(timeout=20)
    assert outcome.get('error') is None
    order=kinds(session_id)
    assert order.index('question_requested')<order.index('question_settled')<order.index('turn_settled')
    settled=payloads(session_id,'question_settled')[0]
    assert settled['outcome']=='selected' and settled['option_id']=='q1:2'
    assert settled['interaction_id']==question['interaction_id']
    assert payloads(session_id,'turn_settled')[0]['stop_reason']=='end_turn'
    assert bridge.get_session(session_id)['status']=='active'


def test_a_permission_is_answered_through_the_bridge(workspace,request_log,adapters,monkeypatch):
    widen_settlement_race(monkeypatch)
    bridge,session_id=started(workspace,request_log,'permission_client',adapters)
    outcome,worker=start_turn(bridge,session_id,'需要授权')
    announced=first_interaction(session_id,'permission_requested')
    assert announced, 'the permission request never reached the event store'
    permission=announced[0]
    assert bridge.answer_permission(session_id,permission['interaction_id'],'perm:1') is True
    worker.join(timeout=20)
    assert outcome.get('error') is None
    order=kinds(session_id)
    assert order.index('permission_requested')<order.index('permission_settled')<order.index('turn_settled')
    assert payloads(session_id,'permission_settled')[0]['outcome']=='selected'


@pytest.mark.parametrize('mode,announced,settled,cancel_method,wire_id',
                         [('ask_client','question_requested','question_settled','cancel_question',WIRE_QUESTION_ID),
                          ('permission_client','permission_requested','permission_settled','cancel_permission',WIRE_PERMISSION_ID)])
def test_no_public_event_ever_carries_the_wire_request_identity(workspace,request_log,adapters,
                                                               mode,announced,settled,cancel_method,wire_id):
    """The peer's own request id is a wire detail. A caller reading the log must never be able to
    address the peer with it, so it must not appear as a field or as a value anywhere."""
    bridge,session_id=started(workspace,request_log,mode,adapters)
    outcome,worker=start_turn(bridge,session_id,'问一句')
    found=first_interaction(session_id,announced)
    assert found
    getattr(bridge,cancel_method)(session_id,found[0]['interaction_id'])
    worker.join(timeout=20)
    for row in rows(session_id):
        values=scalars(row['payload'])
        assert 'request_id' not in values
        assert 'wire_request_id' not in values
        assert wire_id not in values and str(wire_id) not in values


# --- contract 5 + 9: cancel through the bridge, then reuse the session ------

def test_cancel_through_the_bridge_keeps_the_session_usable(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'cancel_aware_once',adapters)
    remote_session_id=bridge.get_session(session_id)['remote_session_id']
    outcome,worker=start_turn(bridge,session_id,'慢慢说')
    assert turn_is_running(request_log)
    answer=bridge.cancel(session_id)
    assert answer['cancelled'] is True and answer['message_id']
    worker.join(timeout=20)
    assert isinstance(outcome.get('error'),CancelledError)
    session=bridge.get_session(session_id)
    assert session['status']=='active'                       # a cancelled turn is not a dead session
    assert session['remote_session_id']==remote_session_id
    assert bridge.status(session_id)['backend']['alive'] is True
    # the same session, through the same bridge, still works
    bridge.send(session_id,'再来一次')
    assert payloads(session_id,'turn_settled')[-1]['stop_reason']=='end_turn'
    assert bridge.get_session(session_id)['remote_session_id']==remote_session_id
    assert kinds(session_id).count('session_started')==1
    assert 'session/cancel' in sent_methods(request_log)


def test_an_idle_bridge_cancel_reports_nothing_to_cancel(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'prompt_multi',adapters)
    assert bridge.cancel(session_id)['cancelled'] is False
    assert 'message_cancel_requested' not in kinds(session_id)


# --- contract 10: a turn failure is not a session failure ------------------

def test_a_turn_error_leaves_the_session_active(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'prompt_fail_once',adapters)
    with pytest.raises(TurnError) as failure:bridge.send(session_id,'坏的一轮')
    assert failure.value.fatal is False
    assert bridge.get_session(session_id)['status']=='active'
    assert 'session_failed' not in kinds(session_id)
    assert payloads(session_id,'message_failed')[0]['fatal'] is False
    bridge.send(session_id,'好的一轮')
    assert payloads(session_id,'turn_settled')[-1]['stop_reason']=='end_turn'
    assert kinds(session_id).count('session_started')==1


def test_only_a_fatal_backend_error_fails_the_session(workspace,request_log):
    bridge,session_id=started(workspace,request_log,'crash_after_prompt')
    with pytest.raises(RuntimeUnavailable) as failure:bridge.send(session_id,'会崩的一轮')
    assert failure.value.fatal is True
    assert bridge.get_session(session_id)['status']=='failed'
    assert 'session_failed' in kinds(session_id)
    with pytest.raises(Exception):bridge.send(session_id,'之后再说')


# --- contract 6: stop through the bridge -----------------------------------

def test_stop_through_the_bridge_ends_everything(workspace,request_log,adapters):
    bridge,session_id=started(workspace,request_log,'ignore_cancel',adapters)
    outcome,worker=start_turn(bridge,session_id,'很久的一轮')
    assert turn_is_running(request_log)
    answer={}
    def stopper():
        started_at=time.perf_counter()
        answer['session']=bridge.stop(session_id)
        answer['seconds']=round(time.perf_counter()-started_at,3)
    thread=threading.Thread(target=stopper,daemon=True);thread.start()
    thread.join(timeout=30)
    worker.join(timeout=30)
    assert not thread.is_alive() and not worker.is_alive()
    assert answer['session']['status']=='stopped'
    # a local stop is not an unexpected loss of the runtime
    assert isinstance(outcome.get('error'),CancelledError)
    assert not isinstance(outcome.get('error'),RuntimeUnavailable)
    assert 'session_failed' not in kinds(session_id)
    order=kinds(session_id)
    assert order.index('session_stopping')<order.index('session_stopped')
    assert bridge.status(session_id)['backend'] is None


# --- contract 1: the bridge only ever speaks the backend contract ----------

class ProtocolOnlyBackend:
    """Exactly the AgentBackend surface. Any other attribute access is recorded and refused, so a
    bridge that quietly reached into a concrete adapter's internals would be caught here."""

    def __init__(self):
        self.violations=[]
        self.calls=[]
        self.release=threading.Event()
        self._sink=lambda event:None

    def __getattr__(self,name):
        self.violations.append(name)
        raise AttributeError('bridge touched a non-contract attribute: '+name)

    def bind(self,emit):self.calls.append('bind');self._sink=emit
    def start(self):self.calls.append('start')
    def send(self,text):
        self.calls.append('send')
        self._sink({'type':'assistant_text_delta','text':'ok'})
        self.release.wait(timeout=10)                        # stays in flight until cancel arrives
        self._sink({'type':'turn_settled','stop_reason':'end_turn'})
    def cancel(self):self.calls.append('cancel');self.release.set();return True
    def answer_question(self,interaction_id,option_id):self.calls.append('answer_question');return True
    def cancel_question(self,interaction_id):self.calls.append('cancel_question');return True
    def answer_permission(self,interaction_id,option_id):self.calls.append('answer_permission');return True
    def cancel_permission(self,interaction_id):self.calls.append('cancel_permission');return True
    def stop(self):self.calls.append('stop')
    def status(self):self.calls.append('status');return {'remote_session_id':'protocol-only','alive':True}


def test_the_bridge_drives_a_backend_through_the_contract_alone():
    """The guard for the boundary itself: this backend exposes nothing else, so if the bridge ever
    reached for an adapter detail the call list would be incomplete or a violation recorded."""
    backend=ProtocolOnlyBackend()
    bridge=AgentBridge(lambda:backend,dataset_id=DATASET)
    session_id=bridge.create_session()['id']
    bridge.start(session_id)
    worker=threading.Thread(target=lambda:bridge.send(session_id,'hi'),daemon=True)
    worker.start()
    assert wait_for(lambda:'send' in backend.calls,10)
    assert bridge.cancel(session_id)['cancelled'] is True
    worker.join(timeout=15)
    bridge.answer_question(session_id,'interaction-1','option-1')
    bridge.cancel_question(session_id,'interaction-1')
    bridge.answer_permission(session_id,'interaction-1','option-1')
    bridge.cancel_permission(session_id,'interaction-1')
    bridge.stop(session_id)
    assert not worker.is_alive()
    assert backend.violations==[]
    assert backend.calls==['bind','start','status','send','cancel','answer_question',
                           'cancel_question','answer_permission','cancel_permission','stop']


def test_the_bridge_source_knows_nothing_about_the_reasonix_wire():
    """A dynamic proof cannot cover every future line, so the protocol literals are checked too."""
    source=(Path(__file__).resolve().parents[1]/'backend'/'agent'/'bridge.py').read_text(encoding='utf-8')
    for token in ('Reasonix','reasonix','jsonrpc','session/prompt','session/cancel','session/close',
                  'mcp-tool:','toolCallId','rawInput'):
        assert token not in source,token


# --- delivery: a broken consumer fails the turn, not the session -----------

def test_a_lost_output_event_fails_the_turn_but_not_the_session(workspace,request_log,adapters,monkeypatch):
    """The adapter's consumer is now the bridge itself, so the newest seam is this one: when the
    bridge cannot store an output event, the turn must fail and the session must survive."""
    bridge,session_id=started(workspace,request_log,'prompt_multi',adapters)
    original=AgentBridge._record
    def refuse_turn_settled(self,row_session_id,event_type,**fields):
        if event_type=='turn_settled':raise RuntimeError('store refused the turn terminal')
        return original(self,row_session_id,event_type,**fields)
    monkeypatch.setattr(AgentBridge,'_record',refuse_turn_settled)
    with pytest.raises(TurnError):bridge.send(session_id,'一轮')
    assert bridge.get_session(session_id)['status']=='active'
    assert 'session_failed' not in kinds(session_id)
    assert payloads(session_id,'message_failed')[0]['fatal'] is False


def test_the_bridge_refuses_a_send_that_never_started(workspace,request_log):
    bridge=make_bridge(workspace,request_log,'prompt_multi')
    session_id=bridge.create_session()['id']
    with pytest.raises(InvalidSessionState):bridge.send(session_id,'还没启动')
    assert 'message_started' not in kinds(session_id)
    # a session that was never started can still be closed, and closing is not a failure
    assert bridge.stop(session_id)['status']=='stopped'
    assert 'session_failed' not in kinds(session_id)
