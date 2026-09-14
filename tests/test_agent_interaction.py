"""Reasonix permission / question: backend-neutral identity, host answers, settlement ordering.

The frames are the real v1.25.1 question shape. Two boundaries are load-bearing here: the peer's
JSON-RPC request id never leaves the adapter, and a settlement can never be overtaken by the turn
terminal it triggers. Permission uses the same method but its tool-level shape is unverified, so
only fields the wire actually carries are ever reported.
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.agent import protocol,reasonix
from backend.agent.backend import TurnError

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='runtime-key-1'
WIRE_QUESTION_ID=900
WIRE_PERMISSION_ID=901


@pytest.fixture(autouse=True)
def short_turn_timeout(monkeypatch):
    """A request that nobody answers must fail fast here instead of waiting out the real budget."""
    monkeypatch.setattr(reasonix,'PROMPT_TIMEOUT_SECONDS',5)


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


def make(workspace,request_log,mode='normal',opened=None,**overrides):
    values=dict(executable=sys.executable,model=MODEL,cwd=str(workspace),
                env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log)),
                runtime_key=RUNTIME_KEY)
    values.update(overrides)
    adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(**values))
    if opened is not None:opened.append(adapter)
    return adapter


def wait_for(produce,timeout=10):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=produce()
        if value:return value
        time.sleep(0.02)
    return None


def of_type(events,kind):
    return [event for event in events if event.get('type')==kind]


def sent_frames(request_log):
    if not request_log.exists():return []
    return [json.loads(line) for line in request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def wire_replies(request_log,wire_id):
    """What the peer actually received as a response to its own request id."""
    return [frame for frame in sent_frames(request_log) if frame.get('id')==wire_id and 'method' not in frame]


def start_turn(adapter,seen):
    """Run one turn in the background so the test thread can answer an interaction."""
    outcome={}
    def drive():
        try:outcome['result']=adapter.send('hi')
        except Exception as error:outcome['error']=error
    worker=threading.Thread(target=drive,daemon=True);worker.start()
    return outcome,worker


def run_turn(workspace,request_log,mode,respond=None,opened=None,timeout=20):
    """Run a turn, answer the first announced interaction, and report what happened."""
    adapter=make(workspace,request_log,mode=mode,opened=opened)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome,worker=start_turn(adapter,seen)
    outcome['adapter']=adapter;outcome['events']=seen
    announced=wait_for(lambda:[event for event in seen
                               if event.get('type') in ('question_requested','permission_requested')],timeout)
    outcome['request']=announced[0] if announced else None
    if announced and respond:
        try:outcome['answer']=respond(adapter,announced[0])
        except Exception as error:outcome['answer_error']=error
    worker.join(timeout=timeout)
    outcome['finished']=not worker.is_alive()
    return outcome


# --- identity boundary -----------------------------------------------------

def test_the_announced_identity_is_not_the_wire_request_id(workspace,request_log,adapters):
    """The peer's JSON-RPC id is a wire detail: it must not become the workbench-facing identity."""
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    interaction_id=outcome['request']['interaction_id']
    assert isinstance(interaction_id,str) and interaction_id
    assert interaction_id!=str(WIRE_QUESTION_ID)
    assert 'request_id' not in outcome['request']
    # the wire reply still uses the peer's own id, because that is all the peer understands
    assert wire_replies(request_log,WIRE_QUESTION_ID)[0]['result']=={'outcome':{'outcome':'cancelled'}}


def test_a_stale_interaction_identity_cannot_answer_a_new_runtime(workspace,request_log,adapters):
    """Request ids are per process and get reused, so an old interaction id must never match a
    new runtime's same-numbered request."""
    first=run_turn(workspace,request_log,'ask_client',
                   lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    stale=first['request']['interaction_id']
    first['adapter'].stop()
    second=run_turn(workspace,request_log,'ask_client',None,adapters,timeout=8)
    fresh=second['request']['interaction_id']
    assert stale!=fresh
    with pytest.raises(ValueError):
        second['adapter'].answer_question(stale,'q1:1')


# --- question normalization ------------------------------------------------

def test_a_question_is_normalized_with_answerable_ids(workspace,request_log,adapters):
    """Only the wire option ids can be answered; the display labels come from rawInput, and an
    option without a display counterpart keeps its own name."""
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    question=dict(outcome['request'])
    assert question.pop('interaction_id')
    assert question=={
        'type':'question_requested','question_id':'q1',
        'question':'Which format should I use?','multi':False,
        'options':[
            {'option_id':'q1:1','label':'表格','description':'用表格呈现'},
            {'option_id':'q1:2','label':'列表','description':'用列表呈现'},
            {'option_id':'q1:cancel','label':'Cancel','description':''}]}


def test_the_display_label_is_never_the_response_value(workspace,request_log,adapters):
    """The wire name ('表格 - 用表格呈现') and the display label ('表格') differ on purpose: using
    the label would send a value the peer never offered."""
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    options=outcome['request']['options']
    assert [option['option_id'] for option in options]==['q1:1','q1:2','q1:cancel']
    assert [option['label'] for option in options]==['表格','列表','Cancel']


# --- question answers ------------------------------------------------------

def test_answering_a_question_lets_the_turn_finish(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.answer_question(request['interaction_id'],'q1:2'),adapters)
    assert outcome.get('error') is None and outcome['finished']
    settled=of_type(outcome['events'],'question_settled')[-1]
    assert settled['interaction_id']==outcome['request']['interaction_id']
    assert settled['outcome']=='selected' and settled['option_id']=='q1:2'
    assert of_type(outcome['events'],'turn_settled')[-1]['stop_reason']=='end_turn'


def test_cancelling_a_question_ends_the_turn_as_a_turn_error(workspace,request_log,adapters):
    """The peer answers a refused question with stopReason=error, which stays a TurnError: only a
    real stopReason=cancelled is a CancelledError."""
    outcome=run_turn(workspace,request_log,'ask_client_cancelled',
                     lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    assert isinstance(outcome.get('error'),TurnError)
    assert of_type(outcome['events'],'question_settled')[-1]['outcome']=='cancelled'
    assert of_type(outcome['events'],'turn_settled')[-1]['stop_reason']=='error'


def test_the_wire_carries_the_selected_option_id(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.answer_question(request['interaction_id'],'q1:1'),adapters)
    assert wire_replies(request_log,WIRE_QUESTION_ID)[0]['result']=={
        'outcome':{'outcome':'selected','optionId':'q1:1'}}


# --- invalid answers -------------------------------------------------------

def test_an_unknown_interaction_id_is_refused(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='ask_client',opened=adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome,worker=start_turn(adapter,seen)
    announced=wait_for(lambda:[event for event in seen if event.get('type')=='question_requested'],10)
    interaction_id=announced[0]['interaction_id']
    with pytest.raises(ValueError):adapter.answer_question('no-such-interaction','q1:1')
    # a bad answer must not consume the real interaction: the right one still works
    assert adapter.diagnostics()['pending_requests']==[interaction_id]
    assert adapter.answer_question(interaction_id,'q1:1') is True
    worker.join(timeout=15)


def test_an_option_the_peer_never_offered_is_refused(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.answer_question(request['interaction_id'],'表格'),adapters)
    assert isinstance(outcome['answer_error'],ValueError)      # the label is not an option id


def test_answering_the_wrong_kind_is_refused(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client',
                     lambda adapter,request:adapter.answer_permission(request['interaction_id'],'q1:1'),adapters)
    assert isinstance(outcome['answer_error'],ValueError)


def test_a_request_can_only_be_answered_once(workspace,request_log,adapters):
    def twice(adapter,request):
        adapter.answer_question(request['interaction_id'],'q1:1')
        return adapter.answer_question(request['interaction_id'],'q1:2')
    outcome=run_turn(workspace,request_log,'ask_client',twice,adapters)
    assert isinstance(outcome['answer_error'],ValueError)


def test_concurrent_answers_claim_the_interaction_exactly_once(workspace,request_log,adapters):
    """The registry is written by the reader thread and read by answering threads, so two answers
    at once must still produce exactly one claim and exactly one wire reply."""
    adapter=make(workspace,request_log,mode='ask_client',opened=adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome,worker=start_turn(adapter,seen)
    announced=wait_for(lambda:[event for event in seen if event.get('type')=='question_requested'],10)
    interaction_id=announced[0]['interaction_id']
    barrier=threading.Barrier(2)
    results={}
    def answer(name,option_id):
        barrier.wait()
        try:results[name]=adapter.answer_question(interaction_id,option_id)
        except Exception as error:results[name]=error
    threads=[threading.Thread(target=answer,args=('a','q1:1')),
             threading.Thread(target=answer,args=('b','q1:2'))]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=10)
    worker.join(timeout=15)
    assert sorted(type(value).__name__ for value in results.values())==['ValueError','bool']
    assert [value for value in results.values() if value is True]==[True]
    assert len(wire_replies(request_log,WIRE_QUESTION_ID))==1
    assert adapter.diagnostics()['pending_requests']==[]
    assert adapter.status()['alive'] is True and adapter.diagnostics()['reader_alive'] is True


# --- settlement ordering and delivery --------------------------------------

def test_a_settlement_is_never_overtaken_by_the_turn_it_triggers(workspace,request_log,adapters,monkeypatch):
    """Answering wakes the peer, whose response can reach the turn terminal thread before the
    answering thread is done. Holding the interaction across the answer and the settle makes that
    impossible; the pause here only widens the window the invariant has to survive."""
    original=reasonix.ReasonixAdapter._reply
    def reply_then_pause(self,wire_request_id,result):
        answered=original(self,wire_request_id,result)
        time.sleep(0.2)
        return answered
    monkeypatch.setattr(reasonix.ReasonixAdapter,'_reply',reply_then_pause)
    order=[]
    adapter=make(workspace,request_log,mode='ask_client',opened=adapters)
    adapter.bind(lambda event:order.append(event.get('type')))
    adapter.start()
    outcome,worker=start_turn(adapter,None)
    wait_for(lambda:[event for event in order if event=='question_requested'],10)
    adapter.answer_question(adapter.diagnostics()['pending_requests'][0],'q1:1')
    worker.join(timeout=15)
    assert order[-3:]==['question_requested','question_settled','turn_settled']


def test_a_lost_settlement_still_fails_the_turn(workspace,request_log,adapters):
    """Delivery failure outranks the protocol outcome: if the settlement never reached the log,
    the caller must not be told the turn ended cleanly."""
    adapter=make(workspace,request_log,mode='ask_client',opened=adapters)
    def sink(event):
        if event.get('type')=='question_settled':raise RuntimeError('consumer exploded')
    adapter.bind(sink)
    adapter.start()
    outcome,worker=start_turn(adapter,None)
    pending=wait_for(lambda:adapter.diagnostics()['pending_requests'] or None,10)
    assert adapter.answer_question(pending[0],'q1:1') is True     # the peer did get its answer
    worker.join(timeout=15)
    assert isinstance(outcome.get('error'),TurnError)
    assert adapter.diagnostics()['emit_failure']['type']=='RuntimeError'
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.status()['alive'] is True


# --- session survival ------------------------------------------------------

def test_a_question_turn_leaves_the_session_usable(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client_once',
                     lambda adapter,request:adapter.answer_question(request['interaction_id'],'q1:1'),adapters)
    adapter=outcome['adapter']
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True
    assert adapter.diagnostics()['pending_requests']==[] and adapter.diagnostics()['in_flight'] is False
    events=[]
    adapter.bind(events.append)
    adapter.send('再问一次')
    assert of_type(events,'turn_settled')[-1]['stop_reason']=='end_turn'
    assert adapter.status()['remote_session_id']=='stub-session'


def test_a_refused_question_leaves_the_session_usable(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'ask_client_cancelled',
                     lambda adapter,request:adapter.cancel_question(request['interaction_id']),adapters)
    adapter=outcome['adapter']
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.diagnostics()['pending_requests']==[] and adapter.diagnostics()['in_flight'] is False
    methods=[frame.get('method') for frame in sent_frames(request_log)]
    assert methods.count('session/new')==1
    assert 'session/resume' not in methods and 'session/load' not in methods


# --- permission ------------------------------------------------------------

def test_a_non_question_request_becomes_a_permission_request(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'permission_client',
                     lambda adapter,request:adapter.cancel_permission(request['interaction_id']),adapters)
    permission=dict(outcome['request'])
    assert permission.pop('interaction_id')
    assert permission=={
        'type':'permission_requested','tool_call_id':'call_perm_1','title':'run_skill',
        'options':[{'option_id':'perm:1','name':'Allow once','kind':'allow_once'},
                   {'option_id':'perm:2','name':'Reject','kind':'reject_once'}]}


def test_a_permission_never_invents_a_tool_or_capability(workspace,request_log,adapters):
    """The real tool-level shape is unverified, so no field may be guessed from the title."""
    outcome=run_turn(workspace,request_log,'permission_client',
                     lambda adapter,request:adapter.cancel_permission(request['interaction_id']),adapters)
    assert set(outcome['request'])<={'type','interaction_id','tool_call_id','title','options'}
    for invented in ('capability_id','server','tool','execution_status','host_status','request_id'):
        assert invented not in outcome['request'],invented


def test_answering_a_permission_uses_the_offered_option(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'permission_client',
                     lambda adapter,request:adapter.answer_permission(request['interaction_id'],'perm:1'),adapters)
    settled=of_type(outcome['events'],'permission_settled')[-1]
    assert settled['outcome']=='selected' and settled['option_id']=='perm:1'
    assert settled['interaction_id']==outcome['request']['interaction_id']
    assert wire_replies(request_log,WIRE_PERMISSION_ID)[0]['result']=={
        'outcome':{'outcome':'selected','optionId':'perm:1'}}


def test_cancelling_a_permission_uses_the_cancel_outcome(workspace,request_log,adapters):
    outcome=run_turn(workspace,request_log,'permission_client',
                     lambda adapter,request:adapter.cancel_permission(request['interaction_id']),adapters)
    assert of_type(outcome['events'],'permission_settled')[-1]['outcome']=='cancelled'
    assert wire_replies(request_log,WIRE_PERMISSION_ID)[0]['result']=={'outcome':{'outcome':'cancelled'}}


# --- malformed / unsupported ----------------------------------------------

def test_a_request_with_nothing_answerable_is_refused_not_ignored(workspace,request_log,adapters):
    """A request carries an id: ignoring it would hang the turn, so it is refused explicitly."""
    outcome=run_turn(workspace,request_log,'malformed_client',None,adapters,timeout=15)
    assert outcome['request'] is None                       # nothing was announced
    assert outcome['adapter'].diagnostics()['refused_requests']=={'malformed_permission':1}
    replies=[frame for frame in sent_frames(request_log) if frame.get('id')==902 and 'method' not in frame]
    assert replies and replies[0]['result']=={'outcome':{'outcome':'cancelled'}}
    assert outcome.get('error') is None                     # the turn still finished cleanly


# --- delivery failure ------------------------------------------------------

@pytest.mark.parametrize('mode,event,wire_id,kind',
                         [('ask_client','question_requested',WIRE_QUESTION_ID,'question'),
                          ('permission_client','permission_requested',WIRE_PERMISSION_ID,'permission')])
def test_an_undelivered_request_is_refused_so_the_peer_never_waits(workspace,request_log,adapters,
                                                                   mode,event,wire_id,kind):
    """If the consumer never saw the request, no answer can arrive: the adapter must refuse it and
    the turn must still fail as a delivery failure."""
    adapter=make(workspace,request_log,mode=mode,opened=adapters)
    def sink(emitted):
        if emitted.get('type')==event:raise RuntimeError('consumer exploded')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError):
        adapter.send('hi')
    assert wire_replies(request_log,wire_id)[0]['result']=={'outcome':{'outcome':'cancelled'}}
    assert adapter.diagnostics()['refused_requests']=={'undelivered_'+kind:1}
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.status()['alive'] is True and adapter.status()['ready'] is True
    assert adapter.diagnostics()['in_flight'] is False


# --- protocol-level helpers ------------------------------------------------

def test_question_detection_is_structural_not_prefix_based():
    """The id prefix is only corroborating evidence; the question text is what decides."""
    real={'toolCall':{'toolCallId':'ask-1-q1','kind':'other',
                      'rawInput':{'id':'q1','multi':False,'options':[],'question':'pick one'}},
          'options':[]}
    assert protocol.permission_is_question(real) is True
    renamed={'toolCall':{'toolCallId':'whatever-9','kind':'other',
                         'rawInput':{'id':'q1','question':'pick one'}},'options':[]}
    assert protocol.permission_is_question(renamed) is True
    permission={'toolCall':{'toolCallId':'ask-1-q1','title':'run_skill'},
                'options':[{'optionId':'p:1','kind':'allow_once'}]}
    assert protocol.permission_is_question(permission) is False
    assert protocol.permission_is_question({}) is False


def test_a_multi_question_is_recognized_as_unsupported():
    multi={'toolCall':{'toolCallId':'ask-2-q1','kind':'other',
                       'rawInput':{'id':'q1','multi':True,'question':'pick','options':[]}},'options':[]}
    assert protocol.permission_is_multi(multi) is True
