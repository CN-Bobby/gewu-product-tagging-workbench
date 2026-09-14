"""Runtime layer: process lifecycle, stdio transport, timeouts. Uses a stub ACP peer, no model.

Covers the 14-point checklist items 7-14 offline and deterministically; the real Reasonix CLI
is exercised separately by scripts/agent_m1_smoke.py so the suite stays network-free.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
import pytest
from backend.agent.runtime import Runtime

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')


def build(mode='normal'):
    events=[];diagnostics=[];lifecycle=[]
    runtime=Runtime(sys.executable,[STUB,mode],on_event=events.append,
        on_diagnostic=lambda kind,line:diagnostics.append((kind,line)),on_lifecycle=lifecycle.append)
    return runtime,events,diagnostics,lifecycle


def wait_until(predicate,timeout=5):
    """Events are emitted from the reader thread, so wait for the effect, not just the process."""
    deadline=time.time()+timeout
    while not predicate() and time.time()<deadline:time.sleep(.05)
    return predicate()

@pytest.fixture
def spawn():
    started=[]
    def factory(mode='normal'):
        runtime,events,diagnostics,lifecycle=build(mode);started.append(runtime);runtime.start()
        return runtime,events,diagnostics,lifecycle
    yield factory
    for runtime in started:runtime.stop()


def test_7_process_starts_and_reports_lifecycle(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    assert runtime.alive and runtime.proc.poll() is None
    assert [event['type'] for event in lifecycle]==['process_starting','process_started']


def test_8_initialize_round_trip(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    result=runtime.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'x','version':'1'}},timeout=10)
    assert result['protocolVersion']==1
    assert 'process_started' in [event['type'] for event in lifecycle]


def test_9_session_new_and_resume_return_a_session_id(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    created=runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    resumed=runtime.request('session/resume',{'sessionId':'kept-session','cwd':'.','mcpServers':[]},timeout=10)
    assert created['sessionId']=='stub-session'
    assert resumed['sessionId']=='kept-session'


def test_10_and_11_prompt_returns_and_streams_text_and_tool_events(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    result=runtime.request('session/prompt',{'sessionId':'s','prompt':[{'type':'text','text':'你好'}]},timeout=10)
    assert result=={'stopReason':'end_turn'}
    texts=[event for event in events if event['method']=='session/update']
    assert len(texts)==4
    kinds=[event['params']['update']['sessionUpdate'] for event in texts]
    assert kinds==['agent_thought_chunk','agent_message_chunk','tool_call','tool_call_update']
    chunks=[event['params']['update']['content']['text'] for event in texts if event['params']['update']['sessionUpdate']=='agent_message_chunk']
    assert chunks==['你好']


def test_11b_remote_error_becomes_a_transport_exception(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    with pytest.raises(RuntimeError,match='stub failure'):
        runtime.request('boom',{},timeout=10)


def test_12_exit_is_detected_and_pending_work_is_failed():
    runtime,events,diagnostics,lifecycle=build('exit');runtime.start()
    deadline=time.time()+5
    while runtime.alive and time.time()<deadline:time.sleep(.05)
    assert not runtime.alive
    with pytest.raises(RuntimeError,match='Reasonix 进程已退出'):
        runtime.send({'jsonrpc':'2.0','method':'x','params':{}})
    assert wait_until(lambda:'reader_stopped' in [event['type'] for event in lifecycle])
    runtime.stop()


def test_12b_a_child_dying_mid_request_releases_the_caller():
    runtime,events,diagnostics,lifecycle=build('crash_after_prompt');runtime.start()
    with pytest.raises(RuntimeError,match='连接已断开'):
        runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=10)
    runtime.stop()


def test_13_stop_terminates_and_is_idempotent(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    runtime.stop()
    assert not runtime.alive and runtime.closed
    runtime.stop()
    assert 'process_stopped' in [event['type'] for event in lifecycle]


def test_14_timeout_does_not_block_forever(spawn):
    runtime,events,diagnostics,lifecycle=spawn('hang')
    started=time.perf_counter()
    with pytest.raises(RuntimeError,match='超时'):
        runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=0.3)
    assert time.perf_counter()-started<5
    assert runtime.alive


def test_parse_failures_are_reported_not_swallowed(spawn):
    runtime,events,diagnostics,lifecycle=spawn('noise')
    runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=10)
    assert wait_until(lambda:len(runtime.protocol_errors())==2)
    kinds=[item['kind'] for item in runtime.protocol_errors()]
    assert kinds==['invalid_json','not_an_object']
    assert [line.rstrip('\n') for kind,line in diagnostics if kind=='stdout']==['not json at all','null']


def test_stderr_lines_are_forwarded_as_diagnostics(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    deadline=time.time()+5
    while not diagnostics and time.time()<deadline:time.sleep(.05)
    assert ('stderr','stub ready\n') in diagnostics


# --- M1.5 observability ---------------------------------------------------

def test_status_fields_are_readable_while_running(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    assert runtime.pid==runtime.proc.pid and runtime.pid>0
    assert runtime.alive and runtime.returncode is None
    assert runtime.started_at and runtime.last_activity_at and runtime.stopped_at is None
    assert runtime.reader_alive


def test_returncode_is_readable_after_a_clean_exit(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    runtime.request('session/close',{'sessionId':'stub-session'},timeout=10)
    deadline=time.time()+5
    while runtime.alive and time.time()<deadline:time.sleep(.05)
    assert runtime.returncode==0
    assert wait_until(lambda:'process_exited' in [event['type'] for event in runtime.lifecycle_tail()])


def test_returncode_is_readable_after_a_crash():
    exited,events,diagnostics,lifecycle=build('exit');exited.start()
    deadline=time.time()+5
    while exited.alive and time.time()<deadline:time.sleep(.05)
    assert exited.returncode==3 and not exited.alive
    exited.stop()

    crashed,events,diagnostics,lifecycle=build('crash_after_prompt');crashed.start()
    with pytest.raises(RuntimeError,match='连接已断开'):
        crashed.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=10)
    deadline=time.time()+5
    while crashed.alive and time.time()<deadline:time.sleep(.05)
    assert crashed.returncode==4
    crashed.stop()


def test_stop_leaves_no_reader_traceback(spawn,monkeypatch):
    caught=[]
    monkeypatch.setattr(threading,'excepthook',lambda args:caught.append(args))
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    runtime.stop()
    assert runtime.join_reader(5)
    time.sleep(.2)
    assert caught==[]


def test_kill_emits_an_explicit_lifecycle_event(spawn):
    runtime,events,diagnostics,lifecycle=spawn('hang')
    runtime.kill()
    deadline=time.time()+5
    while runtime.alive and time.time()<deadline:time.sleep(.05)
    assert 'process_killed' in [event['type'] for event in runtime.lifecycle_tail()]
    assert runtime.returncode is not None
    runtime.stop()


def test_timeout_emits_a_structured_event(spawn):
    runtime,events,diagnostics,lifecycle=spawn('hang')
    with pytest.raises(RuntimeError,match='超时'):
        runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=0.3)
    timeouts=[event for event in runtime.lifecycle_tail() if event['type']=='request_timeout']
    assert len(timeouts)==1
    event=timeouts[0]
    assert event['method']=='session/prompt' and event['timeout']==0.3 and event['ident']==1
    assert event['seconds']>=0.3 and event['timestamp']


def test_stderr_enters_the_ring_buffer(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    deadline=time.time()+5
    while not runtime.diagnostics()['stderr'] and time.time()<deadline:time.sleep(.05)
    entries=runtime.diagnostics()['stderr']
    assert entries[-1]['line']=='stub ready\n' and entries[-1]['timestamp']


def test_malformed_stdout_enters_the_protocol_diagnostic(spawn):
    runtime,events,diagnostics,lifecycle=spawn('noise')
    runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=10)
    time.sleep(.2)
    entries=runtime.diagnostics()['protocol']
    assert [entry['kind'] for entry in entries]==['invalid_json','not_an_object']
    assert all(entry['timestamp'] and entry['raw'] for entry in entries)
    assert 'protocol_error' in [event['type'] for event in runtime.lifecycle_tail()]
    assert runtime.alive


def test_ring_buffers_are_bounded(spawn):
    from backend.agent.runtime import LIFECYCLE_RING_CAPACITY,PROTOCOL_RING_CAPACITY,STDERR_RING_CAPACITY
    runtime,events,diagnostics,lifecycle=spawn('noisy_stderr')
    deadline=time.time()+10
    while len(runtime.diagnostics()['stderr'])<STDERR_RING_CAPACITY and time.time()<deadline:time.sleep(.05)
    stderr=runtime.diagnostics()['stderr']
    assert len(stderr)==STDERR_RING_CAPACITY and stderr[-1]['line']=='noise-299\n'
    assert len(runtime.diagnostics()['lifecycle'])<=LIFECYCLE_RING_CAPACITY
    assert len(runtime.diagnostics()['protocol'])<=PROTOCOL_RING_CAPACITY


def test_chinese_text_survives_the_utf8_transport(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=10)
    chunks=[event['params']['update']['content']['text'] for event in events
            if event['params']['update']['sessionUpdate']=='agent_message_chunk']
    assert chunks==['你好']


def test_stop_leaves_no_reader_thread_behind(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    runtime.stop()
    assert runtime.join_reader(5) and not runtime.reader_alive


def test_lifecycle_events_are_structured(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    runtime.stop()
    runtime.join_reader(5)
    types=[event['type'] for event in lifecycle]
    assert types[0]=='process_starting' and 'process_started' in types
    assert 'process_stopping' in types and 'process_stopped' in types
    for event in lifecycle:assert {'type','timestamp'}<=set(event)
    assert next(event for event in lifecycle if event['type']=='process_started')['pid']==runtime.pid
    assert next(event for event in lifecycle if event['type']=='process_stopped')['returncode'] is not None


# --- inbound request primitive --------------------------------------------

def test_an_inbound_request_can_be_answered_from_the_event_callback():
    """The peer asking us something and us answering it from the reader callback must not
    deadlock: the callback runs without the transport lock held. A short timeout keeps a real
    deadlock failing loudly instead of hanging the suite."""
    runtime,events,diagnostics,lifecycle=build('ask_client')
    runtime.start()
    try:
        seen=[]
        def answer(message):
            seen.append(message)
            runtime.respond(message['id'],{'outcome':{'outcome':'selected','optionId':'q1:1'}})
        runtime.on_event=answer
        result=runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=5)
        assert result=={'stopReason':'end_turn'}          # the turn only ends after our answer
        assert [message['method'] for message in seen]==['session/request_permission']
        assert runtime.alive and runtime.reader_alive
    finally:
        runtime.stop()


def test_the_answered_request_carries_the_original_id():
    """The reply must reuse the inbound id; the stub only finishes the turn when it sees it."""
    log_path=Path(tempfile.mkdtemp(prefix='stub-reply-'))/'requests.jsonl'
    runtime=Runtime(sys.executable,[STUB,'ask_client',str(log_path)],on_event=None)
    runtime.start()
    try:
        def answer(message):
            assert message['id']==900
            runtime.respond(message['id'],{'outcome':{'outcome':'cancelled'}})
        runtime.on_event=answer
        assert runtime.request('session/prompt',{'sessionId':'s','prompt':[]},timeout=5)=={'stopReason':'end_turn'}
        frames=[json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        replies=[frame for frame in frames if frame.get('id')==900 and 'method' not in frame]
        assert replies and replies[0]['result']=={'outcome':{'outcome':'cancelled'}}
        assert runtime.alive and runtime.reader_alive
    finally:
        runtime.stop()
