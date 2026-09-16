"""Reasonix turn lifecycle: cancel, stop, timeouts and the structured error mapping.

Cancel abandons a turn; stop tears the runtime down. Neither claims that dispatched work stopped,
so nothing here waits for a tool to finish. Fatality is decided from structured facts the runtime
already reports (liveness, connection state, our own cancel/stop intent) — never from a message.
"""
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.agent import protocol,reasonix
from backend.agent.backend import CancelledError, RuntimeUnavailable, TurnError

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='runtime-key-1'


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


@pytest.fixture(autouse=True)
def short_turn_timeout(monkeypatch):
    """An unanswered turn must fail fast here instead of waiting out the real budget."""
    monkeypatch.setattr(reasonix,'PROMPT_TIMEOUT_SECONDS',5)


def make(workspace,request_log,mode='normal',opened=None):
    adapter=reasonix.ReasonixAdapter(reasonix.ReasonixLaunchConfig(
        executable=sys.executable,model=MODEL,cwd=str(workspace),
        env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log),
                 STUB_ONCE_FILE=str(workspace/'session-once.json')),runtime_key=RUNTIME_KEY))
    if opened is not None:opened.append(adapter)
    return adapter


def wait_for(produce,timeout=10):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=produce()
        if value:return value
        time.sleep(0.02)
    return None


def live_readers():
    return [thread.name for thread in threading.enumerate() if thread.name.startswith('reasonix-acp')]


def sent_methods(request_log):
    if not request_log.exists():return []
    import json
    return [json.loads(line).get('method') for line in
            request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def start_turn(adapter,text='hi'):
    outcome={}
    def drive():
        try:outcome['result']=adapter.send(text)
        except Exception as error:outcome['error']=error
    worker=threading.Thread(target=drive,daemon=True);worker.start()
    return outcome,worker


# --- cancel ----------------------------------------------------------------

def test_cancelling_an_active_turn_abandons_it(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='cancel_aware',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    started=time.perf_counter()
    assert adapter.cancel() is True
    assert time.perf_counter()-started<1                      # a notification, not a wait
    worker.join(timeout=15)
    assert isinstance(outcome.get('error'),CancelledError)
    assert outcome['error'].fatal is False
    # The turn was abandoned, not the session: the interrupted turn's runtime is finished — it may
    # still deliver that turn's frames — and the session identity is kept for the next send to resume.
    assert adapter.status()['alive'] is False
    assert adapter.status()['remote_session_id']=='stub-session'
    assert wait_until(lambda:'session/cancel' in sent_methods(request_log))
    # the runtime is finished as part of the interrupted turn's unwind, not left for the next send
    assert wait_until(lambda:'session/close' in sent_methods(request_log))


def test_the_session_still_works_after_a_cancel(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='cancel_aware_once',opened=adapters)
    events=[]
    adapter.bind(events.append)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    adapter.cancel()
    worker.join(timeout=15)
    assert isinstance(outcome.get('error'),CancelledError)
    events.clear()
    adapter.send('再来一次')
    assert [event for event in events if event.get('type')=='turn_settled'][-1]['stop_reason']=='end_turn'
    assert adapter.status()['remote_session_id']=='stub-session'
    assert adapter.diagnostics()['in_flight'] is False


def test_an_idle_cancel_is_harmless_and_repeat_cancel_is_a_no_op(workspace,request_log,adapters):
    idle=make(workspace,request_log,mode='normal',opened=adapters)
    idle.bind(lambda event:None)
    idle.start()
    assert idle.cancel() is False                             # nothing in flight
    assert idle.status()['ready'] is True
    # A peer that never abandons its turn keeps the turn in flight, so the repeat is deterministic:
    # one notification, and a second cancel that neither raises nor sends anything again.
    adapter=make(workspace,request_log,mode='ignore_cancel',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    assert adapter.cancel() is True
    assert adapter.cancel() is True                           # idempotent while the turn unwinds
    # The log is written by the peer, so it trails the notify by a pipe round trip.
    assert wait_until(lambda:sent_methods(request_log).count('session/cancel')==1)
    adapter.stop()
    worker.join(timeout=15)
    assert not worker.is_alive()


# --- stop ------------------------------------------------------------------

def test_stopping_an_idle_session_closes_and_tears_down(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='normal',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    adapter.stop()
    assert 'session/close' in sent_methods(request_log)
    assert adapter.status()['alive'] is False
    assert adapter.status()['pid'] is None
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_readers())


def wait_until(predicate,timeout=5):
    deadline=time.time()+timeout
    while not predicate() and time.time()<deadline:time.sleep(0.05)
    return predicate()


def test_stopping_during_a_turn_does_not_wait_for_the_turn(workspace,request_log,adapters):
    """The peer refuses to abandon its turn, so only the teardown can end this. It must still be
    bounded, and the caller must not be told the runtime failed unexpectedly."""
    adapter=make(workspace,request_log,mode='ignore_cancel',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    started=time.perf_counter()
    adapter.stop()
    elapsed=time.perf_counter()-started
    assert elapsed<4                                          # bounded, and nowhere near a tool's run
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert isinstance(outcome.get('error'),CancelledError)     # local stop, not an unexpected loss
    assert not isinstance(outcome.get('error'),RuntimeUnavailable)
    assert wait_until(lambda:'session/cancel' in sent_methods(request_log))   # it abandoned first
    assert adapter.status()['alive'] is False
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_readers())


def test_stop_is_idempotent_and_send_after_stop_is_refused(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='normal',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    adapter.stop()
    adapter.stop()                                            # must not restart or raise
    assert adapter.status()['alive'] is False
    with pytest.raises(RuntimeUnavailable):adapter.send('hi')
    assert 'session/new' not in sent_methods(request_log)[2:]  # no session was recreated


# --- timeout ---------------------------------------------------------------

def test_a_prompt_timeout_abandons_the_turn_without_killing_the_session(workspace,request_log,adapters,monkeypatch):
    monkeypatch.setattr(reasonix,'PROMPT_TIMEOUT_SECONDS',2)
    adapter=make(workspace,request_log,mode='hang_once',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    started=time.perf_counter()
    with pytest.raises(TurnError) as failure:adapter.send('hi')
    assert not isinstance(failure.value,RuntimeUnavailable)
    assert time.perf_counter()-started<20
    assert wait_until(lambda:'session/cancel' in sent_methods(request_log))  # abandoned first
    assert adapter.status()['alive'] is False and adapter.status()['remote_session_id']=='stub-session'
    # the session is healthy, so the next turn must work — on a fresh runtime resumed to it
    events=[]
    adapter.bind(events.append)
    adapter.send('再来一次')
    assert [event for event in events if event.get('type')=='turn_settled'][-1]['stop_reason']=='end_turn'


def test_an_unexpected_runtime_death_is_runtime_unavailable(workspace,request_log,adapters):
    """A dead process is a lost session; that is what stays fatal, and it is decided by liveness."""
    adapter=make(workspace,request_log,mode='crash_after_prompt',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    with pytest.raises(RuntimeUnavailable):adapter.send('hi')
    assert adapter.status()['ready'] is False and adapter.status()['alive'] is False


def test_a_missing_stop_reason_without_a_local_cancel_is_a_turn_error(workspace,request_log,adapters):
    """An unexplained ending is not a success, and it is not a session failure either."""
    adapter=make(workspace,request_log,mode='prompt_missing_stop',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    with pytest.raises(TurnError) as failure:adapter.send('hi')
    assert not isinstance(failure.value,RuntimeUnavailable)
    assert adapter.status()['ready'] is True


def test_a_missing_stop_reason_after_a_local_cancel_is_a_cancellation(workspace,request_log,adapters):
    """The peer's compatibility reply omits the reason; we asked for the cancel, so it is one."""
    adapter=make(workspace,request_log,mode='cancel_aware_no_stop',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    adapter.cancel()
    worker.join(timeout=15)
    assert isinstance(outcome.get('error'),CancelledError)
    assert adapter.status()['alive'] is False
    assert adapter.status()['remote_session_id']=='stub-session'


# --- an unreachable runtime -------------------------------------------------

def test_a_dead_reader_on_a_live_process_is_runtime_unavailable(workspace,request_log,adapters,monkeypatch):
    """A live process is not a live session. Once the reader is gone nothing can ever be answered,
    so this runtime is finished for every future request — not just the turn that noticed.

    The reader is broken with no request in flight, so its death is fully settled before send()
    runs and the mapping is decided without racing the reader thread's last instructions.
    """
    monkeypatch.setattr(reasonix,'PROMPT_TIMEOUT_SECONDS',2)
    monkeypatch.setattr(reasonix,'CLOSE_TIMEOUT_SECONDS',1)
    original=reasonix.ReasonixAdapter._on_message
    def explode_on_update(self,message):
        if protocol.method_of(message)==protocol.SESSION_UPDATE:raise RuntimeError('reader exploded')
        return original(self,message)
    monkeypatch.setattr(reasonix.ReasonixAdapter,'_on_message',explode_on_update)
    adapter=make(workspace,request_log,mode='unsolicited_update',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    assert wait_until(lambda:adapter.diagnostics()['reader_alive'] is False)
    assert adapter.status()['alive'] is True                  # the process itself is still running
    with pytest.raises(RuntimeUnavailable) as failure:adapter.send('hi')
    assert failure.value.fatal is True
    assert adapter.status()['ready'] is False                 # the session is finished too
    assert adapter.status()['alive'] is True


# --- cancel with a waiting interaction -------------------------------------

def test_cancelling_a_turn_that_waits_for_an_answer_resolves_the_interaction(workspace,request_log,adapters):
    """A pending question must be settled before the terminal it is cancelled by, and must stop
    being answerable afterwards. The peer ends this turn with an error, which is exactly the case
    the local cancel intent exists for: the caller asked to abandon, so it hears a cancellation."""
    adapter=make(workspace,request_log,mode='ask_client_cancelled',opened=adapters)
    order=[]
    adapter.bind(lambda event:order.append(event))
    adapter.start()
    outcome,worker=start_turn(adapter)
    announced=wait_for(lambda:[event for event in order if event.get('type')=='question_requested'],10)
    interaction_id=announced[0]['interaction_id']
    adapter.cancel()
    worker.join(timeout=15)
    assert isinstance(outcome.get('error'),CancelledError)
    kinds=[event.get('type') for event in order]
    assert kinds.index('question_settled')<kinds.index('turn_settled')
    assert adapter.diagnostics()['pending_requests']==[]
    with pytest.raises(ValueError):adapter.answer_question(interaction_id,'q1:1')


def test_stopping_while_a_question_waits_still_tears_down(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='ask_client',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['pending_requests'] or None,10)
    adapter.stop()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert adapter.status()['alive'] is False
    assert adapter.diagnostics()['pending_requests']==[]
    assert adapter.diagnostics()['exit']['alive'] is False
    assert wait_until(lambda:not live_readers())


# --- tool contract is not polluted by cancellation -------------------------

def test_a_cancelled_turn_never_reports_a_real_tool_failure(workspace,request_log,adapters):
    """The host's "context canceled" is not evidence that the tool failed, so the tool contract
    keeps reporting only the host lifecycle — and the old fabricated failure name stays gone."""
    adapter=make(workspace,request_log,mode='cancel_aware_tool',opened=adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    # Wait for the peer's tool frames before interrupting: an interrupted turn's later output is
    # fenced off by contract, so the tool contract has to be observed while the turn is still live.
    assert wait_until(lambda:[event for event in seen if event.get('type')=='tool_call_settled'])
    adapter.cancel()
    worker.join(timeout=15)
    kinds=[event.get('type') for event in seen]
    assert 'tool_call_failed' not in kinds
    assert kinds==['assistant_text_delta','tool_call_started','tool_call_settled','turn_settled']
    assert seen[2]['host_status']=='failed'                    # the host's fact, nothing more
    assert isinstance(outcome.get('error'),CancelledError)


# --- concurrency -----------------------------------------------------------

def test_stop_and_send_racing_leave_nothing_behind(workspace,request_log,adapters):
    """Both threads must finish, the runtime must really be gone, and no reader may survive."""
    for _ in range(5):
        adapter=make(workspace,request_log,mode='ignore_cancel',opened=adapters)
        adapter.bind(lambda event:None)
        adapter.start()
        outcome,worker=start_turn(adapter)
        assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
        stopper=threading.Thread(target=adapter.stop,daemon=True);stopper.start()
        stopper.join(timeout=10)
        worker.join(timeout=10)
        assert not stopper.is_alive() and not worker.is_alive()
        assert adapter.status()['alive'] is False
        assert adapter.diagnostics()['exit']['alive'] is False
        assert wait_until(lambda:not live_readers())


def test_cancel_and_send_racing_leave_the_session_usable(workspace,request_log,adapters):
    adapter=make(workspace,request_log,mode='cancel_aware',opened=adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome,worker=start_turn(adapter)
    assert wait_for(lambda:adapter.diagnostics()['in_flight'] or None,10)
    canceller=threading.Thread(target=adapter.cancel,daemon=True);canceller.start()
    canceller.join(timeout=10)
    worker.join(timeout=15)
    assert not canceller.is_alive() and not worker.is_alive()
    assert isinstance(outcome.get('error'),CancelledError)
    assert adapter.status()['alive'] is False and adapter.diagnostics()['in_flight'] is False
    assert adapter.status()['remote_session_id']=='stub-session'
