"""Runtime layer: process lifecycle, stdio transport, timeouts. Uses a stub ACP peer, no model.

Covers the 14-point checklist items 7-14 offline and deterministically; the real Reasonix CLI
is exercised separately by scripts/agent_m1_smoke.py so the suite stays network-free.
"""
import sys
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
    assert lifecycle==['started']


def test_8_initialize_round_trip(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    result=runtime.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'x','version':'1'}},timeout=10)
    assert result['protocolVersion']==1
    assert lifecycle==['started']


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
    time.sleep(.2)
    assert 'reader_stopped' in lifecycle
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
    assert 'stopped' in lifecycle


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
    time.sleep(.2)
    kinds=[item['kind'] for item in runtime.protocol_errors()]
    assert kinds==['invalid_json','not_an_object']
    assert [line.rstrip('\n') for kind,line in diagnostics if kind=='stdout']==['not json at all','null']


def test_stderr_lines_are_forwarded_as_diagnostics(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    deadline=time.time()+5
    while not diagnostics and time.time()<deadline:time.sleep(.05)
    assert ('stderr','stub ready\n') in diagnostics
