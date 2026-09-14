"""Reasonix adapter (M3B-1): handshake, new session, status and teardown.

Offline and deterministic: the peer is tests/fixtures/stub_acp_cli.py, so no model, no network
and no quota. The workspace and the request log are temporary, and nothing here reads the
workbench's own data directory.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.agent import reasonix
from backend.agent.backend import AgentBackend,CancelledError,RuntimeUnavailable,TurnError

REPO=Path(__file__).resolve().parents[1]
STUB=Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py'
SOURCE=Path(reasonix.__file__).read_text(encoding='utf-8')
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='runtime-key-1'
# The real v1.25.1 handshake advertises exactly this name; the adapter must read it, not guess it.
RELOAD_METHOD='_reasonix.io/session/reloadExtensions'

FORBIDDEN_IMPORTS=('harness','conversation','db','runtime_isolation','mcp_server',
                   'business_questions','conversation_presentation','tagging_rules')
# The adapter must not reach for the legacy caller. The CLI subcommand string is a different
# thing entirely and is expected, so this looks at identifiers rather than at raw text.
FORBIDDEN_IDENTIFIERS=('acp','storage','draft','harness','sqlalchemy')


def wait_until(predicate,timeout=5):
    deadline=time.time()+timeout
    while not predicate() and time.time()<deadline:time.sleep(.05)
    return predicate()


def live_reader_threads():
    return [thread.name for thread in threading.enumerate() if thread.name.startswith('reasonix-acp')]


@pytest.fixture
def workspace(tmp_path):
    """A temporary session workspace.

    The adapter builds ``<executable> acp --model <model>``, so the stub is placed where a real
    binary would have to be for that command line to work: reachable as ``acp`` from the
    workspace the session is opened in.
    """
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


def frames(request_log):
    if not request_log.exists():return []
    return [json.loads(line) for line in request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def make(workspace,request_log,mode='normal',opened=None,**overrides):
    values=dict(executable=sys.executable,model=MODEL,cwd=str(workspace),
                env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log)),
                runtime_key=RUNTIME_KEY)
    values.update(overrides)
    adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(**values))
    if opened is not None:opened.append(adapter)
    return adapter


def resume_state(session_id='persisted-session-1',runtime_key=RUNTIME_KEY):
    return reasonix.ReasonixResumeState(session_id=session_id,runtime_key=runtime_key)


def collect(adapter):
    """Bind a sink that keeps every normalized event in arrival order."""
    events=[]
    adapter.bind(events.append)
    return events


def kinds(events):
    return [event.get('type') for event in events]


def imported_targets():
    names=set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node,ast.Import):
            for alias in node.names:names.add(alias.name)
        elif isinstance(node,ast.ImportFrom):
            names.add(('.'*node.level)+(node.module or ''))
            for alias in node.names:names.add(alias.name)
    return names


def identifiers():
    names=set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node,ast.Name):names.add(node.id)
        elif isinstance(node,ast.Attribute):names.add(node.attr)
        elif isinstance(node,ast.alias):names.add((node.asname or node.name).split('.')[-1])
    return {name.lower() for name in names}


# --- 1-3: contract, binding, start ----------------------------------------

def test_1_adapter_satisfies_the_backend_contract(workspace,request_log):
    assert isinstance(make(workspace,request_log),AgentBackend)


def test_2_bind_registers_an_event_sink(workspace,request_log):
    adapter=make(workspace,request_log)
    adapter.bind(lambda event:None)
    with pytest.raises(TypeError):adapter.bind('not callable')


def test_3_start_brings_the_runtime_up(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    state=adapter.status()
    assert state['alive'] is True and state['ready'] is True and isinstance(state['pid'],int)


# --- 4-6: initialize wire format ------------------------------------------

def test_4_to_6_initialize_wire_format(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    first=frames(request_log)[0]
    assert first['method']=='initialize'
    assert first['params']['protocolVersion']==1
    assert first['params']['clientCapabilities']=={}
    assert first['params']['clientInfo']['name']
    assert first['params']['clientInfo']['version']
    assert adapter.request_methods()[:1]==['initialize']


# --- 7-9: session/new wire format -----------------------------------------

def test_7_to_9_session_new_wire_format(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    second=frames(request_log)[1]
    assert second['method']=='session/new'
    assert second['params']['cwd']==str(workspace)
    assert second['params']['mcpServers']==[]
    assert adapter.request_methods()==['initialize','session/new']


def test_session_new_carries_the_configured_cwd_verbatim(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    cwd=frames(request_log)[1]['params']['cwd']
    assert cwd==str(workspace)
    assert Path(cwd).is_absolute() and Path(cwd).is_dir()


def test_cwd_must_be_absolute(workspace,request_log):
    with pytest.raises(ValueError):make(workspace,request_log,cwd='relative/cwd')


def test_cwd_must_already_exist(workspace,tmp_path,request_log):
    with pytest.raises(ValueError):make(workspace,request_log,cwd=str(tmp_path/'missing'))


def test_launch_command_derives_from_the_configured_model(workspace,request_log,adapters):
    adapter=make(workspace,request_log,model='deepseek/deepseek-v4-flash-vision-exp',opened=adapters)
    assert adapter.launch_args()==['acp','--model','deepseek/deepseek-v4-flash-vision-exp']
    assert adapter.status()['model']=='deepseek/deepseek-v4-flash-vision-exp'


# --- 10-12: remote session id and status ----------------------------------

def test_10_remote_session_id_comes_from_the_peer(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    assert adapter.diagnostics()['remote_session_id']=='stub-session'


def test_11_status_reports_the_remote_session_id(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    assert adapter.status()['remote_session_id']=='stub-session'


def test_12_status_reports_pid_alive_and_ready(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    state=adapter.status()
    assert {'provider','model','remote_session_id','runtime_key','pid','alive','ready'}<=set(state)
    assert state['provider']=='reasonix'
    assert state['model']==MODEL
    assert state['alive'] is True and state['ready'] is True and isinstance(state['pid'],int)


def test_status_pid_is_none_once_the_runtime_is_gone(workspace,request_log):
    """status() reports the current runtime only; the history lives in diagnostics()."""
    adapter=make(workspace,request_log)
    adapter.start()
    live_pid=adapter.status()['pid']
    assert isinstance(live_pid,int)
    adapter.stop()
    assert adapter.status()['pid'] is None
    assert adapter.status()['alive'] is False
    history=adapter.diagnostics()['exit']
    assert history['pid']==live_pid
    assert history['alive'] is False
    assert 'returncode' in history


def test_status_pid_stops_reporting_a_process_that_died(workspace,request_log):
    """status() must test liveness, not merely that a runtime object is attached.

    White-box on purpose: the window under test is "process gone, runtime still attached",
    which only exists between an unexpected exit and the next stop().
    """
    adapter=make(workspace,request_log)
    adapter.start()
    live_pid=adapter.status()['pid']
    adapter._runtime.kill()
    assert wait_until(lambda:adapter.status()['alive'] is False)
    assert adapter._runtime.pid==live_pid   # the stale pid is still reachable internally
    assert adapter.status()['pid'] is None


def test_status_pid_is_none_after_a_failed_start(workspace,request_log):
    adapter=make(workspace,request_log,mode='initialize_fail')
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.status()['pid'] is None
    assert adapter.status()['alive'] is False


# --- 13-17: the adapter has no legacy coupling -----------------------------

@pytest.mark.parametrize('forbidden',FORBIDDEN_IMPORTS)
def test_13_to_16_adapter_does_not_import_business_modules(forbidden):
    flat={name.lstrip('.').split('.')[-1] for name in imported_targets()}
    assert forbidden not in flat


def test_13_to_16_importing_the_adapter_pulls_in_no_business_module():
    """Static imports can lie about transitivity, so import it for real in a clean interpreter."""
    prefixes=('backend.db','backend.harness','backend.conversation','backend.runtime_isolation',
              'backend.mcp_server','backend.business_questions')
    script=('import sys;sys.path.insert(0,%r);import backend.agent.reasonix as m;'
            'print(",".join(sorted(n for n in sys.modules if n.startswith(%r))))' % (str(REPO),prefixes))
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,cwd=str(REPO))
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='',result.stdout


def test_17_no_reference_to_the_legacy_caller():
    assert 'acp' in SOURCE   # the Reasonix launch subcommand itself is allowed
    for name in FORBIDDEN_IDENTIFIERS:
        assert name not in identifiers(),name


# --- 18-21: start-stage error mapping -------------------------------------

def test_18_initialize_failure_is_runtime_unavailable(workspace,request_log):
    adapter=make(workspace,request_log,mode='initialize_fail')
    with pytest.raises(RuntimeUnavailable):adapter.start()


def test_19_no_process_or_reader_survives_initialize_failure(workspace,request_log):
    adapter=make(workspace,request_log,mode='initialize_fail')
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.status()['alive'] is False
    assert adapter.status()['pid'] is None
    assert adapter.diagnostics()['exit']['alive'] is False
    assert adapter.diagnostics()['reader_alive'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_20_session_new_failure_is_runtime_unavailable(workspace,request_log):
    adapter=make(workspace,request_log,mode='session_new_fail')
    with pytest.raises(RuntimeUnavailable):adapter.start()


def test_21_no_process_or_reader_survives_session_new_failure(workspace,request_log):
    adapter=make(workspace,request_log,mode='session_new_fail')
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.status()['alive'] is False
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_20b_missing_executable_is_runtime_unavailable(workspace,request_log):
    adapter=make(workspace,request_log,executable=str(workspace/'not-a-real-binary.exe'))
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.status()['alive'] is False
    assert adapter.status()['pid'] is None


def test_initialize_protocol_version_is_negotiated(workspace,request_log):
    adapter=make(workspace,request_log,mode='protocol_version_mismatch')
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert frames(request_log)[0]['params']['protocolVersion']==1
    assert adapter.status()['ready'] is False
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_reader_threads())


# --- 22-25: teardown ------------------------------------------------------

def test_22_stop_sends_session_close(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    adapter.stop()
    assert adapter.request_methods()==['initialize','session/new','session/close']
    assert frames(request_log)[2]['params']=={'sessionId':'stub-session'}


def test_23_runtime_is_not_alive_after_stop(workspace,request_log):
    adapter=make(workspace,request_log)
    adapter.start()
    adapter.stop()
    assert adapter.status()['alive'] is False
    assert adapter.diagnostics()['exit']['alive'] is False


def test_24_stop_is_idempotent(workspace,request_log):
    adapter=make(workspace,request_log)
    adapter.start()
    adapter.stop()
    adapter.stop()
    assert adapter.status()['alive'] is False


def test_25_reader_threads_do_not_survive_stop(workspace,request_log):
    adapter=make(workspace,request_log)
    adapter.start()
    assert adapter.diagnostics()['reader_alive'] is True
    adapter.stop()
    assert wait_until(lambda:not live_reader_threads())


def test_a_nonzero_exit_code_on_close_is_not_a_failure(workspace,request_log):
    """The peer is known to exit non-zero after session/close; that is a normal teardown."""
    adapter=make(workspace,request_log,mode='close_exit_1')
    adapter.start()
    adapter.stop()
    assert adapter.diagnostics()['exit']['returncode']==1
    assert adapter.status()['alive'] is False


# --- 26-27: not implemented yet, and explicitly so -------------------------

def test_26_send_is_implemented_and_completes_a_turn(workspace,request_log,adapters):
    """M3B-1 asserted send was unimplemented; M3B-3 implements it, so this slot now asserts the
    opposite contract rather than disappearing."""
    adapter=make(workspace,request_log,opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert kinds(events).count('turn_settled')==1


def test_27_cancel_is_explicitly_not_implemented(workspace,request_log):
    adapter=make(workspace,request_log)
    with pytest.raises(NotImplementedError):adapter.cancel()


# --- M3B-2: resuming a persisted session ----------------------------------

def test_resume_runs_initialize_resume_and_reload(workspace,request_log,adapters):
    adapter=make(workspace,request_log,resume=resume_state(),opened=adapters)
    adapter.start()
    assert adapter.request_methods()==['initialize','session/resume',RELOAD_METHOD]
    assert adapter.status()['ready'] is True
    assert adapter.status()['remote_session_id']=='persisted-session-1'
    assert adapter.diagnostics()['started_as']=='resumed'
    assert adapter.diagnostics()['reload_method']==RELOAD_METHOD


def test_resume_sends_the_configured_cwd_and_no_servers(workspace,request_log,adapters):
    adapter=make(workspace,request_log,resume=resume_state(),opened=adapters)
    adapter.start()
    frame=frames(request_log)[1]
    assert frame['method']=='session/resume'
    assert frame['params']=={'sessionId':'persisted-session-1','cwd':str(workspace),'mcpServers':[]}


def test_resume_never_calls_session_new_or_load(workspace,request_log,adapters):
    adapter=make(workspace,request_log,resume=resume_state(),opened=adapters)
    adapter.start()
    methods=adapter.request_methods()
    assert 'session/new' not in methods and 'session/load' not in methods


def test_new_path_never_resumes_and_never_reloads(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    adapter.start()
    assert adapter.request_methods()==['initialize','session/new']
    assert adapter.diagnostics()['started_as']=='new'
    assert adapter.diagnostics()['reload_method'] is None


def test_resume_identity_comes_from_the_request_not_the_echo(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='resume_no_echo',
                 resume=resume_state(session_id='requested-id'),opened=adapters)
    adapter.start()
    assert adapter.status()['remote_session_id']=='requested-id'


def test_resume_capability_is_required(workspace,request_log):
    adapter=make(workspace,request_log,mode='no_resume_capability',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize']
    assert adapter.status()['ready'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_reload_method_is_read_before_resuming(workspace,request_log):
    """Without the advertised vendor method there is nothing safe to call, so nothing is."""
    adapter=make(workspace,request_log,mode='no_reload_capability',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize']
    assert wait_until(lambda:not live_reader_threads())


def test_resume_error_tears_down(workspace,request_log):
    adapter=make(workspace,request_log,mode='resume_fail',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize','session/resume']
    assert adapter.status()['ready'] is False and adapter.status()['alive'] is False
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_resume_invalid_result_tears_down_before_reloading(workspace,request_log):
    adapter=make(workspace,request_log,mode='resume_invalid',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize','session/resume']
    assert adapter.status()['ready'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_reload_error_tears_down(workspace,request_log):
    adapter=make(workspace,request_log,mode='reload_fail',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize','session/resume',RELOAD_METHOD]
    assert adapter.status()['ready'] is False and adapter.status()['alive'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_reload_invalid_result_tears_down(workspace,request_log):
    adapter=make(workspace,request_log,mode='reload_invalid',resume=resume_state())
    with pytest.raises(RuntimeUnavailable):adapter.start()
    assert adapter.request_methods()==['initialize','session/resume',RELOAD_METHOD]
    assert adapter.status()['ready'] is False
    assert wait_until(lambda:not live_reader_threads())


def test_runtime_key_must_not_be_blank(workspace,request_log):
    with pytest.raises(ValueError):make(workspace,request_log,runtime_key='   ')


def test_runtime_key_mismatch_fails_before_any_process_or_request(workspace,request_log):
    with pytest.raises(ValueError):
        make(workspace,request_log,resume=resume_state(runtime_key='a-different-key'))
    assert not request_log.exists()
    assert not live_reader_threads()


def test_resume_with_a_matching_key_is_allowed(workspace,request_log,adapters):
    adapter=make(workspace,request_log,resume=resume_state(runtime_key=RUNTIME_KEY),opened=adapters)
    adapter.start()
    assert adapter.status()['ready'] is True
    assert adapter.status()['runtime_key']==RUNTIME_KEY


def test_resume_needs_a_session_id(workspace,request_log):
    with pytest.raises(ValueError):
        make(workspace,request_log,
             resume=reasonix.ReasonixResumeState(session_id='   ',runtime_key=RUNTIME_KEY))


def test_stop_on_a_resumed_session_closes_and_is_idempotent(workspace,request_log,adapters):
    adapter=make(workspace,request_log,resume=resume_state(),opened=adapters)
    adapter.start()
    adapter.stop()
    adapter.stop()
    assert adapter.request_methods()==['initialize','session/resume',RELOAD_METHOD,'session/close']
    assert frames(request_log)[3]['params']=={'sessionId':'persisted-session-1'}
    assert adapter.status()['alive'] is False


# --- M3B-3: send, streaming and turn outcome -------------------------------

def test_send_before_start_is_refused(workspace,request_log):
    adapter=make(workspace,request_log)
    collect(adapter)
    with pytest.raises(RuntimeUnavailable):adapter.send('hi')
    assert not request_log.exists()


def test_send_on_a_dead_runtime_is_runtime_unavailable(workspace,request_log):
    adapter=make(workspace,request_log)
    collect(adapter)
    adapter.start()
    adapter._runtime.kill()
    assert wait_until(lambda:adapter.status()['alive'] is False)
    with pytest.raises(RuntimeUnavailable):adapter.send('hi')


def test_blank_text_is_refused_without_any_request(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    collect(adapter)
    adapter.start()
    with pytest.raises(ValueError):adapter.send('   ')
    assert [f.get('method') for f in frames(request_log)]==['initialize','session/new']


def test_send_uses_the_prompt_wire_shape_and_the_current_session(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    collect(adapter)
    adapter.start()
    adapter.send('hello')
    prompts=[f for f in frames(request_log) if f.get('method')=='session/prompt']
    assert len(prompts)==1
    assert prompts[0]['params']=={'sessionId':'stub-session',
                                  'prompt':[{'type':'text','text':'hello'}]}
    assert adapter.status()['remote_session_id']=='stub-session'


def test_one_send_never_opens_a_second_session_or_prompt(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    collect(adapter)
    adapter.start()
    adapter.send('hello')
    methods=[f.get('method') for f in frames(request_log)]
    assert methods.count('session/prompt')==1
    assert 'session/new' not in methods[2:] and 'session/resume' not in methods


def test_a_single_delta_is_emitted_once(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert [e['text'] for e in events if e.get('type')=='assistant_text_delta']==['你好']


def test_deltas_keep_their_order_and_their_text(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    deltas=[e['text'] for e in events if e.get('type')=='assistant_text_delta']
    assert deltas==['你','好','M3B3','_OK']
    assert ''.join(deltas)=='你好M3B3_OK'
    assert adapter.diagnostics()['turn_chars']==len('你好M3B3_OK')


def test_each_delta_is_emitted_immediately_not_coalesced(workspace,request_log,adapters):
    """The bridge owns coalescing, so four chunks must arrive as four separate events."""
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert len([e for e in events if e.get('type')=='assistant_text_delta'])==4


def test_turn_settled_comes_after_the_last_delta_and_only_once(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    order=kinds(events)
    assert order.count('turn_settled')==1
    assert order[-1]=='turn_settled'
    assert order.index('turn_settled')>max(
        index for index,kind in enumerate(order) if kind=='assistant_text_delta')


def test_turn_settled_carries_the_normalized_stop_reason(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    settled=[e for e in events if e.get('type')=='turn_settled']
    assert settled[0]['stop_reason']=='end_turn'


def test_a_settled_turn_leaves_the_session_ready_and_sendable(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('one')
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True
    assert adapter.diagnostics()['in_flight'] is False
    adapter.send('two')
    prompts=[f for f in frames(request_log) if f.get('method')=='session/prompt']
    assert [p['params']['prompt'][0]['text'] for p in prompts]==['one','two']
    assert {p['params']['sessionId'] for p in prompts}=={'stub-session'}
    assert adapter.status()['remote_session_id']=='stub-session'


def test_a_second_send_while_one_is_in_flight_is_refused(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_slow',opened=adapters)
    events=collect(adapter)
    adapter.start()
    first=threading.Thread(target=lambda:adapter.send('one'),daemon=True)
    first.start()
    assert wait_until(lambda:adapter.diagnostics()['in_flight'] is True)
    with pytest.raises(TurnError):adapter.send('two')
    first.join(timeout=15)
    assert adapter.diagnostics()['in_flight'] is False
    assert [f.get('method') for f in frames(request_log)].count('session/prompt')==1
    assert kinds(events).count('turn_settled')==1


def test_unmapped_updates_never_become_assistant_text(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_extra',opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert [e['text'] for e in events if e.get('type')=='assistant_text_delta']==['only-text']
    ignored=adapter.diagnostics()['ignored_updates']
    assert ignored.get('agent_thought_chunk')==1
    assert ignored.get('available_commands_update')==1
    # a tool frame is classified now, so it must not be counted as an unmapped update
    assert 'tool_call' not in ignored
    assert '_reasonix.io/session/status_update' in adapter.diagnostics()['inbox']


def test_a_prompt_error_with_a_live_runtime_is_only_a_turn_error(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_fail',opened=adapters)
    events=collect(adapter)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    assert adapter.status()['ready'] is True
    assert adapter.status()['alive'] is True
    assert adapter.diagnostics()['in_flight'] is False
    assert 'turn_settled' not in kinds(events)


def test_a_turn_error_still_allows_the_next_send(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_fail',opened=adapters)
    events=collect(adapter)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('one')
    with pytest.raises(TurnError):adapter.send('two')
    assert [f.get('method') for f in frames(request_log)].count('session/prompt')==2
    assert adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['remote_session_id']=='stub-session'


def test_runtime_death_during_a_turn_is_runtime_unavailable(workspace,request_log):
    adapter=make(workspace,request_log,mode='crash_after_prompt')
    events=collect(adapter)
    adapter.start()
    with pytest.raises(RuntimeUnavailable):adapter.send('hi')
    assert adapter.status()['ready'] is False
    assert adapter.status()['alive'] is False
    assert adapter.diagnostics()['in_flight'] is False
    assert 'turn_settled' not in kinds(events)


def test_a_sink_that_raises_ends_the_turn_as_a_turn_error(workspace,request_log,adapters):
    """A consumer failure is a delivery failure, not a transport failure."""
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    seen=[]
    def sink(event):
        seen.append(event.get('type'))
        raise RuntimeError('sink exploded')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    assert adapter.diagnostics()['emit_failure']['type']=='RuntimeError'
    assert adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['ready'] is True
    assert adapter.status()['alive'] is True
    assert adapter.status()['remote_session_id']=='stub-session'
    assert 'turn_settled' not in seen


def test_a_raising_sink_still_lets_the_prompt_response_land(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    adapter.bind(lambda event:(_ for _ in ()).throw(RuntimeError('sink exploded')))
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    completed=[event for event in adapter.diagnostics()['runtime']['lifecycle']
               if event.get('type')=='request_completed' and event.get('method')=='session/prompt']
    assert completed
    assert not [event for event in adapter.diagnostics()['runtime']['lifecycle']
                if event.get('type') in ('request_failed','request_timeout')]


def test_a_raising_sink_does_not_kill_the_reader_or_the_process(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    adapter.bind(lambda event:(_ for _ in ()).throw(RuntimeError('sink exploded')))
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.status()['alive'] is True
    assert not [event for event in adapter.diagnostics()['runtime']['lifecycle']
                if event.get('type') in ('reader_failed','reader_closed')]


def test_a_broken_sink_is_called_once_per_turn_not_once_per_event(workspace,request_log,adapters):
    """Four deltas plus a settle must not become five exceptions."""
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    calls={'n':0}
    def sink(event):
        calls['n']+=1
        raise RuntimeError('sink exploded')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    assert calls['n']==1


def test_the_emit_failure_is_reset_for_the_next_turn(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    seen=[]
    def sink(event):
        seen.append(event.get('type'))
        if seen.count('assistant_text_delta')==1:raise RuntimeError('only the first time')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('one')
    assert adapter.diagnostics()['emit_failure']['type']=='RuntimeError'
    adapter.send('two')
    assert adapter.diagnostics()['emit_failure'] is None
    assert adapter.diagnostics()['in_flight'] is False
    assert seen.count('turn_settled')==1
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True


def test_the_emit_boundary_only_absorbs_ordinary_exceptions(workspace,request_log):
    """White-box on purpose: the guarantee is about which exception classes are absorbed."""
    class Fatal(BaseException):pass
    adapter=make(workspace,request_log)
    adapter.bind(lambda event:(_ for _ in ()).throw(Fatal('not an Exception')))
    with pytest.raises(Fatal):adapter._emit({'type':'assistant_text_delta','text':'x'})
    assert adapter.diagnostics()['emit_failure'] is None


def test_a_turn_settled_only_sink_failure_still_fails_the_turn(workspace,request_log,adapters):
    """Streaming can succeed while the turn outcome is still undelivered: losing only the turn
    event is still a delivery failure, so send() must not report success."""
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    seen=[]
    def sink(event):
        seen.append(event.get('type'))
        if event.get('type')=='turn_settled':raise RuntimeError('turn event exploded')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('hi')
    assert seen.count('assistant_text_delta')==4            # every delta was accepted
    assert seen.count('turn_settled')==1                    # and the turn event was attempted
    assert adapter.diagnostics()['emit_failure']['type']=='RuntimeError'
    settled=[event for event in adapter.diagnostics()['runtime']['lifecycle']
             if event.get('type')=='request_completed' and event.get('method')=='session/prompt']
    assert settled
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['ready'] is True
    assert adapter.status()['alive'] is True
    assert adapter.status()['remote_session_id']=='stub-session'


def test_a_turn_after_a_turn_settled_delivery_failure_can_succeed(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_multi',opened=adapters)
    def bad(event):
        if event.get('type')=='turn_settled':raise RuntimeError('turn event exploded')
    adapter.bind(bad)
    adapter.start()
    with pytest.raises(TurnError):adapter.send('one')
    events=collect(adapter)                                 # a healthy consumer for the next turn
    adapter.send('two')
    assert kinds(events).count('turn_settled')==1
    assert adapter.diagnostics()['emit_failure'] is None
    assert adapter.diagnostics()['in_flight'] is False
    methods=[frame.get('method') for frame in frames(request_log)]
    assert methods.count('session/new')==1
    assert methods.count('session/prompt')==2
    assert 'session/resume' not in methods and 'session/load' not in methods


# --- M3B-4C: turn outcome mapping ------------------------------------------

def run_turn(workspace,request_log,mode,opened):
    """Run one turn and hand back the adapter, its events and whatever it raised."""
    adapter=make(workspace,request_log,mode=mode,opened=opened)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    error=None
    try:adapter.send('hi')
    except Exception as raised:error=raised
    return adapter,seen,error


def settled(seen):
    return [event for event in seen if event.get('type')=='turn_settled']


def test_an_end_turn_settles_and_returns_normally(workspace,request_log,adapters):
    adapter,seen,error=run_turn(workspace,request_log,'normal',adapters)
    assert error is None
    assert settled(seen)[-1]['stop_reason']=='end_turn'
    assert adapter.status()['ready'] is True


def test_a_cancelled_turn_settles_then_raises_cancelled_error(workspace,request_log,adapters):
    """The turn outcome is delivered first, so the log keeps it even though send() fails."""
    adapter,seen,error=run_turn(workspace,request_log,'prompt_cancelled',adapters)
    assert isinstance(error,CancelledError) and not isinstance(error,TurnError)
    assert settled(seen)[-1]['stop_reason']=='cancelled'
    assert [event['type'] for event in seen][-1]=='turn_settled'
    assert adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['ready'] is True
    assert adapter.status()['alive'] is True


def test_an_unknown_stop_reason_is_passed_through_then_fails_the_turn(workspace,request_log,adapters):
    """An unrecognised reason is reported verbatim and counted; it is not a session failure."""
    adapter,seen,error=run_turn(workspace,request_log,'prompt_unknown_stop',adapters)
    assert isinstance(error,TurnError) and not isinstance(error,CancelledError)
    assert settled(seen)[-1]['stop_reason']=='future_reason'
    assert adapter.diagnostics()['unknown_stop_reasons']=={'future_reason':1}
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True


@pytest.mark.parametrize('mode',['prompt_missing_stop','prompt_null_stop','prompt_numeric_stop'])
def test_a_missing_stop_reason_settles_without_the_field_and_fails_the_turn(workspace,request_log,adapters,mode):
    adapter,seen,error=run_turn(workspace,request_log,mode,adapters)
    assert isinstance(error,TurnError) and not isinstance(error,CancelledError)
    assert 'stop_reason' not in settled(seen)[-1]
    assert adapter.diagnostics()['unknown_stop_reasons']=={'missing':1}
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True


def test_a_cancelled_turn_leaves_the_session_usable_for_the_next_turn(workspace,request_log,adapters):
    adapter,seen,error=run_turn(workspace,request_log,'prompt_cancelled_once',adapters)
    assert isinstance(error,CancelledError)
    state=adapter.status()
    assert state['ready'] is True and state['alive'] is True
    assert state['remote_session_id']=='stub-session'
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.diagnostics()['in_flight'] is False
    events=collect(adapter)
    adapter.send('再问一次')
    assert settled(events)[-1]['stop_reason']=='end_turn'
    assert adapter.status()['remote_session_id']=='stub-session'
    methods=[frame.get('method') for frame in frames(request_log)]
    assert methods.count('session/new')==1 and methods.count('session/prompt')==2
    assert 'session/resume' not in methods and 'session/load' not in methods


def test_delivery_failure_outranks_a_cancelled_outcome(workspace,request_log,adapters):
    """Delivery has precedence: the caller must not be told the turn ended cleanly when part of
    its outward contract never reached the log."""
    adapter=make(workspace,request_log,mode='prompt_cancelled',opened=adapters)
    def sink(event):
        if event.get('type')=='turn_settled':raise RuntimeError('turn event exploded')
    adapter.bind(sink)
    adapter.start()
    with pytest.raises(TurnError) as failure:adapter.send('hi')
    assert not isinstance(failure.value,CancelledError)
    assert adapter.diagnostics()['emit_failure']['type']=='RuntimeError'
    assert adapter.diagnostics()['reader_alive'] is True
    assert adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['ready'] is True and adapter.status()['alive'] is True


def test_a_cancelled_turn_keeps_the_tool_fact_and_the_turn_fact_separate(workspace,request_log,adapters):
    """The host reporting a failed tool call and the host cancelling the turn are two independent
    facts, and neither may be folded into the other."""
    adapter,seen,error=run_turn(workspace,request_log,'prompt_cancelled_with_tool',adapters)
    assert isinstance(error,CancelledError)
    assert [event['type'] for event in seen]==['assistant_text_delta','tool_call_started',
                                              'tool_call_settled','turn_settled']
    assert seen[2]['host_status']=='failed'
    assert seen[3]['stop_reason']=='cancelled'


# --- M3B-4A: tool classification feeds diagnostics -------------------------

TOOL_EVENT_NAMES=('tool_call_started','tool_call_settled')


def test_tool_frames_are_classified_for_diagnostics(workspace,request_log,adapters):
    adapter=make(workspace,request_log,opened=adapters)
    collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert adapter.diagnostics()['tool_classes']=={'mcp_tool_call':1}
    assert adapter.diagnostics()['tool_frames_seen']==1


def test_a_non_mcp_capability_call_is_counted_without_a_tool_event(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='prompt_extra',opened=adapters)
    events=collect(adapter)
    adapter.start()
    adapter.send('hi')
    assert not [kind for kind in kinds(events) if kind in TOOL_EVENT_NAMES]
    assert adapter.diagnostics()['tool_classes']=={'other_capability':1}
