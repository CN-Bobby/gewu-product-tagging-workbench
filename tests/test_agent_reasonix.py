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
from backend.agent.backend import AgentBackend, RuntimeUnavailable

REPO=Path(__file__).resolve().parents[1]
STUB=Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py'
SOURCE=Path(reasonix.__file__).read_text(encoding='utf-8')
MODEL='deepseek/deepseek-v4-flash'

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
                env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log)))
    values.update(overrides)
    adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(**values))
    if opened is not None:opened.append(adapter)
    return adapter


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
    assert {'provider','model','remote_session_id','pid','alive','ready'}<=set(state)
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

def test_26_send_is_explicitly_not_implemented(workspace,request_log):
    adapter=make(workspace,request_log)
    adapter.bind(lambda event:None)
    with pytest.raises(NotImplementedError):adapter.send('你好')


def test_27_cancel_is_explicitly_not_implemented(workspace,request_log):
    adapter=make(workspace,request_log)
    with pytest.raises(NotImplementedError):adapter.cancel()


def test_resuming_an_existing_session_is_rejected_in_this_step(workspace,request_log):
    with pytest.raises(NotImplementedError):
        make(workspace,request_log,remote_session_id='existing-session')
