"""Runtime layer: process lifecycle, stdio transport, timeouts. Uses a stub ACP peer, no model.

Covers the 14-point checklist items 7-14 offline and deterministically; the real Reasonix CLI
is exercised separately by scripts/agent_m1_smoke.py so the suite stays network-free.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
import pytest
from backend.agent.runtime import ProcessJob,Runtime

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
WINDOWS=os.name=='nt'
# A venv ``python.exe`` is a launcher that starts the base interpreter as its own child, so a test
# that spawns ``sys.executable`` would be watching the launcher while the peer runs in a process the
# OS created around it. These tests need the pid they associate to be the process that matters.
PEER_PYTHON=getattr(sys,'_base_executable',None) or sys.executable
SYNCHRONIZE=0x00100000;PROCESS_QUERY_LIMITED_INFORMATION=0x1000;STILL_ACTIVE=259
_kernel32=ctypes.WinDLL('kernel32',use_last_error=True) if WINDOWS else None
_leaked=[]


def alive(pid):
    """Independent OS answer, so a test never has to trust this layer's own bookkeeping."""
    handle=_kernel32.OpenProcess(SYNCHRONIZE|PROCESS_QUERY_LIMITED_INFORMATION,False,int(pid))
    if not handle:return False
    try:
        code=wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle,ctypes.byref(code)):return False
        return code.value==STILL_ACTIVE
    finally:_kernel32.CloseHandle(handle)


def in_job(pid):
    """Whether the OS reports this process inside the job it was given."""
    handle=_kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,False,int(pid))
    if not handle:return False
    try:
        result=wintypes.BOOL()
        if not _kernel32.IsProcessInJob(handle,None,ctypes.byref(result)):return False
        return bool(result)
    finally:_kernel32.CloseHandle(handle)


def children_of(pid):
    out=subprocess.run(['wmic','process','where','ParentProcessId=%d'%int(pid),'get','ProcessId,Name'],
                       capture_output=True,text=True,encoding='utf-8',errors='replace').stdout
    rows=[]
    for line in out.splitlines()[1:]:
        parts=line.split()
        if len(parts)==2 and parts[1].isdigit():rows.append((int(parts[1]),parts[0]))
    return rows


def exits_within(pid,seconds):
    deadline=time.time()+seconds
    while time.time()<deadline:
        if not alive(pid):return True
        time.sleep(.05)
    return not alive(pid)


def hard_kill(pid):
    """Exact pid only: nothing in this suite ever kills by image name."""
    subprocess.run(['taskkill','/F','/PID',str(int(pid))],capture_output=True,text=True)


def sleeper():
    """A child that never reads stdin, so only OS-level ownership can end it."""
    process=subprocess.Popen([PEER_PYTHON,'-c','import time;time.sleep(600)'],
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    _leaked.append(process.pid)
    return process


@pytest.fixture(autouse=True)
def no_surviving_children():
    yield
    for pid in list(_leaked):
        if alive(pid):hard_kill(pid)
        _leaked.remove(pid)


def build(mode='normal',python=None):
    events=[];diagnostics=[];lifecycle=[]
    runtime=Runtime(python or PEER_PYTHON,[STUB,mode],on_event=events.append,
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


# --- M6-1A: the runtime owns its process tree -----------------------------------------------
#
# The peer normally notices a dead parent by itself, because its stdin pipe closes and the read
# returns EOF. That is the peer's behaviour, not something this layer guarantees; these tests fix
# the guarantee in the layer that spawns the process.

@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T1_closing_the_last_job_handle_ends_a_process_that_never_reads_stdin():
    """KILL_ON_JOB_CLOSE, with the same child shape left outside the job as a control."""
    owned=sleeper()
    control=sleeper()
    job=ProcessJob()
    assert job.create() is True
    assert job.assign(owned.pid) is True
    assert alive(owned.pid) and alive(control.pid)
    try:
        job.release()
        assert exits_within(owned.pid,10),'a job member must not outlive the last handle'
        assert alive(control.pid),'the control proves it was the job, not the child shape'
    finally:
        job.release()
        for process in (owned,control):
            if alive(process.pid):hard_kill(process.pid)
            process.wait(timeout=5)


@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T2_a_started_runtime_reports_and_actually_holds_the_job(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    job=runtime.diagnostics()['job']
    assert job=={'supported':True,'created':True,'assigned':True,'error':None}
    # not the helper's word for it: the OS is asked about the process that was actually spawned
    assert in_job(runtime.proc.pid) is True
    assert 'job_unavailable' not in [event['type'] for event in lifecycle]


@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T3_the_whole_tree_is_covered_without_naming_any_of_it(spawn):
    """A descendant the peer starts after association dies with the job, not by being identified."""
    runtime,events,diagnostics,lifecycle=spawn('spawn_child')
    # The association window: the peer exists and is associated, and nothing has been written to it
    # yet. It owns nothing at this point except the console host the OS gives every console process,
    # which is not a workbench process and is not this layer's to manage.
    assert [name for _,name in children_of(runtime.proc.pid)] in ([],['conhost.exe']), \
        'no descendant may exist before the first request'
    runtime.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'x','version':'1'}},timeout=10)
    runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    assert wait_until(lambda:[line for kind,line in diagnostics if 'child-pid=' in line])
    child_pid=int(re.search(r'child-pid=(\d+)',
                            [line for kind,line in diagnostics if 'child-pid=' in line][0]).group(1))
    _leaked.append(child_pid)
    assert alive(child_pid) and in_job(child_pid) is True
    runtime.stop()
    assert exits_within(child_pid,10)
    assert not alive(runtime.proc.pid)


@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T4_a_host_that_refuses_the_association_degrades_instead_of_failing(monkeypatch):
    """Hardening is optional; the agent session is not. The reason stays observable."""
    monkeypatch.setattr(ProcessJob,'assign',lambda self,pid:(
        setattr(self,'assign_attempted',True),self._fail('AssignProcessToJobObject failed (5)'))[1])
    runtime,events,diagnostics,lifecycle=build()
    try:
        runtime.start()
        assert runtime.alive
        assert runtime.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'x','version':'1'}},timeout=10)['protocolVersion']==1
        job=runtime.diagnostics()['job']
        assert job['created'] is True and job['assigned'] is False
        assert 'AssignProcessToJobObject failed' in job['error']
        assert 'job_unavailable' in [event['type'] for event in lifecycle]
        assert runtime._job.handle is None,'the created handle must not be left open'
    finally:
        runtime.stop()


def test_T4b_a_host_without_jobs_says_so_and_keeps_working(monkeypatch):
    monkeypatch.setattr('backend.agent.runtime.os.name','posix')
    runtime,events,diagnostics,lifecycle=build()
    try:
        runtime.start()
        assert runtime.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'x','version':'1'}},timeout=10)['protocolVersion']==1
        assert runtime.diagnostics()['job']=={'supported':False,'created':False,'assigned':False,'error':None}
        assert 'job_unavailable' not in [event['type'] for event in lifecycle]
    finally:
        runtime.stop()


@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T5_stopping_releases_the_job_and_stays_idempotent(spawn):
    runtime,events,diagnostics,lifecycle=spawn()
    assert runtime.diagnostics()['job']['assigned'] is True
    runtime.stop();runtime.stop()
    assert runtime._job.handle is None
    assert [event['type'] for event in lifecycle].count('process_stopped')==1


@pytest.mark.skipif(not WINDOWS,reason='Windows job objects')
def test_T6_a_spawn_failure_leaves_no_job_handle_and_no_reader(monkeypatch):
    def boom(*args,**kwargs):raise OSError('spawn refused')
    monkeypatch.setattr('backend.agent.runtime.subprocess.Popen',boom)
    runtime,events,diagnostics,lifecycle=build()
    with pytest.raises(OSError):runtime.start()
    assert runtime._job.handle is None,'a failed start must not leak the job handle'
    assert runtime.proc is None and runtime.reader_alive is False
