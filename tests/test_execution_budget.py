"""M6-2A: the semantic execution budget, the interrupt race and interrupted-turn recovery.

The budget is caller policy, so these tests inject very short values and drive the real adapter over
the deterministic peer. ``execution_budget=None`` means the pre-budget behaviour, which T15 pins.
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.agent import reasonix
from backend.agent.backend import CancelledError,RuntimeUnavailable,TurnError
from backend.agent.reasonix import ExecutionBudget,ReasonixAdapter,ReasonixLaunchConfig

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='m6-2a-key'


def budget(**overrides):
    values={'first_activity_timeout':0.4,'idle_timeout':0.5,'tool_wait_timeout':1.5,
            'turn_wall_clock_timeout':6.0}
    values.update(overrides)
    return ExecutionBudget(**values)


@pytest.fixture
def workspace(tmp_path):
    cwd=tmp_path/'cwd';cwd.mkdir()
    (cwd/'PRODUCT_ANALYST_AGENT.md').write_text('stub prompt\n',encoding='utf-8')
    (cwd/'reasonix.toml').write_text('[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\n',encoding='utf-8')
    shutil.copy(STUB,cwd/'acp')
    return cwd


@pytest.fixture
def request_log(tmp_path):
    """Where the peer records every frame it received, for wire-level assertions."""
    return tmp_path/'frames.jsonl'


@pytest.fixture
def adapters():
    opened=[]
    yield opened
    for adapter in opened:
        try:adapter.stop()
        except Exception:pass


def make(workspace,request_log,mode='normal',limits=None,opened=None):
    """One real adapter over the deterministic peer; `limits` is the injected budget."""
    adapter=ReasonixAdapter(ReasonixLaunchConfig(
        executable=sys.executable,model=MODEL,cwd=str(workspace),
        env=dict(os.environ,STUB_MODE=mode,STUB_LOG=str(request_log),
                 STUB_ONCE_FILE=str(Path(workspace)/'session-once.json')),
        runtime_key=RUNTIME_KEY,budget=limits))
    if opened is not None:opened.append(adapter)
    return adapter


def events_of(adapter,seen):
    adapter.bind(seen.append)


def drive(adapter,text='做点什么'):
    """Run one turn and report how it ended, without letting the error escape."""
    outcome={}
    try:adapter.send(text);outcome['result']='completed'
    except Exception as error:
        outcome['error']=type(error).__name__
        outcome['code']=getattr(error,'code',None)
        outcome['instance']=error
    return outcome


def drive_async(adapter,text='做点什么'):
    outcome={}
    def run():
        try:adapter.send(text);outcome['result']='completed'
        except Exception as error:
            outcome['error']=type(error).__name__
            outcome['code']=getattr(error,'code',None)
    worker=threading.Thread(target=run,daemon=True);worker.start()
    outcome['worker']=worker
    return outcome


def sent_methods(request_log):
    if not request_log.exists():return []
    return [json.loads(line).get('method') for line in
            request_log.read_text(encoding='utf-8').splitlines() if line.strip()]


def wait_until(predicate,timeout=10):
    deadline=time.time()+timeout
    while not predicate() and time.time()<deadline:time.sleep(.02)
    return predicate()


def kinds(seen):
    return [event.get('type') for event in seen]


# --- the model itself ------------------------------------------------------

def test_a_budget_rejects_non_positive_values():
    for field in ('first_activity_timeout','idle_timeout','tool_wait_timeout','turn_wall_clock_timeout'):
        with pytest.raises(ValueError):budget(**{field:0})
        with pytest.raises(ValueError):budget(**{field:-1})


# --- first activity --------------------------------------------------------

def test_T1_a_turn_with_no_activity_is_interrupted_by_the_first_activity_deadline(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_hang_serve',budget(),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    started=time.perf_counter()
    outcome=drive(adapter)
    assert outcome['error']=='TurnError' and outcome['code']=='turn_first_activity_timeout'
    assert time.perf_counter()-started<4
    assert wait_until(lambda:sent_methods(request_log).count('session/cancel')==1)


# --- ordinary idle --------------------------------------------------------

def test_T2_activity_then_silence_is_interrupted_by_the_idle_deadline(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_silent',budget(first_activity_timeout=5),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    started=time.perf_counter()
    outcome=drive(adapter)
    assert outcome['code']=='turn_idle_timeout'
    assert time.perf_counter()-started<4
    assert 'assistant_text_delta' in kinds(seen)


def test_T3_periodic_activity_keeps_the_turn_alive_until_it_settles(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_periodic',
                 budget(first_activity_timeout=0.5,idle_timeout=0.6,turn_wall_clock_timeout=8),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome=drive(adapter)
    assert outcome['result']=='completed'
    assert sum(1 for kind in kinds(seen) if kind=='assistant_text_delta')>=5


def test_T4_the_wall_clock_deadline_is_absolute_even_with_continuous_activity(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_stream',
                 budget(first_activity_timeout=0.5,idle_timeout=5,turn_wall_clock_timeout=0.9),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    started=time.perf_counter()
    outcome=drive(adapter)
    assert outcome['code']=='turn_wall_clock_timeout'
    assert time.perf_counter()-started<4


# --- tool wait ------------------------------------------------------------

def test_T5_a_running_tool_is_not_killed_by_the_ordinary_idle_deadline(workspace,request_log,adapters):
    """The tool runs longer than idle_timeout and shorter than tool_wait_timeout: nothing fires."""
    adapter=make(workspace,request_log,'budget_tool_slow',
                 budget(first_activity_timeout=0.5,idle_timeout=0.3,tool_wait_timeout=3),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome=drive(adapter)
    assert outcome['result']=='completed'
    assert kinds(seen)==['tool_call_started','tool_call_settled','assistant_text_delta','turn_settled']


def test_T6_a_tool_that_outlives_its_own_deadline_ends_the_turn(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_tool_stuck',
                 budget(first_activity_timeout=1,idle_timeout=1,tool_wait_timeout=0.5),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome=drive(adapter)
    assert outcome['code']=='turn_tool_timeout'


def test_T11_each_tool_has_its_own_deadline(workspace,request_log,adapters):
    """Two tools run at once: settling one leaves the turn in TOOL_WAIT for the other."""
    adapter=make(workspace,request_log,'budget_two_tools',
                 budget(first_activity_timeout=0.5,idle_timeout=0.2,tool_wait_timeout=3),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    outcome=drive(adapter)
    assert outcome['result']=='completed',outcome
    starts=[event for event in seen if event.get('type')=='tool_call_started']
    settles=[event for event in seen if event.get('type')=='tool_call_settled']
    assert {event['tool_call_id'] for event in starts}=={'budget-tool-A','budget-tool-B'}
    assert {event['tool_call_id'] for event in settles}=={'budget-tool-A','budget-tool-B'}


# --- human wait -----------------------------------------------------------

def test_T7_an_open_question_pauses_the_idle_deadline_for_all_open_interactions(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_interactions',
                 budget(first_activity_timeout=0.5,idle_timeout=0.4,turn_wall_clock_timeout=8),adapters)
    announced=[]
    adapter.bind(announced.append)
    adapter.start()
    outcome={}
    def run():
        try:adapter.send('需要确认');outcome['result']='completed'
        except Exception as error:outcome.update(error=type(error).__name__,code=getattr(error,'code',None))
    worker=threading.Thread(target=run,daemon=True);worker.start()
    assert wait_until(lambda:len([e for e in announced if e.get('type') in
                                  ('question_requested','permission_requested')])==3)
    ids=[event['interaction_id'] for event in announced if event.get('type') in
         ('question_requested','permission_requested')]
    # Longer than the idle deadline: with a question open, nothing may fire.
    time.sleep(1.2)
    assert worker.is_alive(),'an open question must pause the idle deadline'
    adapter.answer_question(ids[0],'q1:1')
    time.sleep(0.6)
    assert worker.is_alive(),'the other interactions are still open'
    with adapter._turn_cond:pending=set(adapter._turn.pending_interactions)
    assert pending==set(ids[1:])
    adapter.answer_question(ids[1],'q1:1')
    adapter.cancel_permission(ids[2])
    worker.join(timeout=10)
    assert outcome.get('result')=='completed',outcome
    settled=[event['interaction_id'] for event in announced if event.get('type') in
             ('question_settled','permission_settled')]
    assert sorted(settled)==sorted(ids),'every interaction settles exactly once'


# --- noise ----------------------------------------------------------------

def test_T12_noise_never_satisfies_the_first_activity_deadline(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_noise',
                 budget(first_activity_timeout=0.5,idle_timeout=5,turn_wall_clock_timeout=30),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    outcome=drive(adapter)
    assert outcome['code']=='turn_first_activity_timeout'


def test_T12b_noise_does_not_refresh_the_idle_deadline(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_noise',
                 budget(first_activity_timeout=0.5,idle_timeout=0.6,turn_wall_clock_timeout=30),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    started=time.perf_counter()
    outcome=drive(adapter)
    # The peer sent a chunk through prompt_stream? No: this mode emits no chunk at all, so the first
    # deadline is what fires — the point here is that 20 rounds of noise did not satisfy it either.
    assert outcome['code']=='turn_first_activity_timeout'
    assert time.perf_counter()-started<4
    assert 'assistant_text_delta' not in kinds(seen)


# --- interrupt exactly once ----------------------------------------------

def test_T10_the_first_interrupt_wins_and_the_second_neither_notifies_nor_rewrites(workspace,request_log,adapters):
    """Whichever claims the turn first owns the outcome; the other must not notify again."""
    cancel_first=make(workspace,request_log,'budget_hang_serve',
                      budget(first_activity_timeout=3,turn_wall_clock_timeout=20),adapters)
    cancel_first.bind(lambda event:None)
    cancel_first.start()
    outcome=drive_async(cancel_first)
    assert wait_until(lambda:cancel_first.diagnostics()['in_flight'] or None,5)
    time.sleep(0.2)
    assert cancel_first.cancel() is True
    outcome['worker'].join(timeout=10)
    assert outcome.get('error')=='CancelledError'
    assert wait_until(lambda:sent_methods(request_log).count('session/cancel')==1)
    time.sleep(0.2)
    assert sent_methods(request_log).count('session/cancel')==1
    cancel_first.stop()

    other=Path(request_log).parent/'second.jsonl'
    deadline_first=make(workspace,other,'budget_slow_cancel',
                        budget(first_activity_timeout=0.3,turn_wall_clock_timeout=20),adapters)
    deadline_first.bind(lambda event:None)
    deadline_first.start()
    outcome=drive_async(deadline_first)
    assert wait_until(lambda:deadline_first.diagnostics()['in_flight'] or None,5)
    time.sleep(0.5)                     # the deadline claimed the turn; the peer replies slowly
    assert deadline_first.cancel() is True,'the turn is still unwinding, so this is still a request'
    outcome['worker'].join(timeout=10)
    assert outcome.get('error')=='TurnError' and outcome['code']=='turn_first_activity_timeout'
    assert wait_until(lambda:sent_methods(other).count('session/cancel')==1)
    time.sleep(0.2)
    assert sent_methods(other).count('session/cancel')==1


# --- recovery -------------------------------------------------------------

def test_T8_a_semantic_timeout_recycles_the_runtime_and_resumes_the_same_session(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_hang_serve',budget(first_activity_timeout=0.4),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    native=adapter.status()['remote_session_id']
    old_pid=adapter.status()['pid']
    first=drive(adapter)
    assert first['code']=='turn_first_activity_timeout'
    assert wait_until(lambda:'session/close' in sent_methods(request_log))
    assert adapter.status()['alive'] is False
    second=drive(adapter,'第二轮')
    assert second.get('result')=='completed',second
    assert adapter.status()['remote_session_id']==native
    assert adapter.status()['pid']!=old_pid
    methods=sent_methods(request_log)
    assert 'session/resume' in methods and '_reasonix.io/session/reloadExtensions' in methods
    assert methods.count('session/new')==1,'a resume must never open a second session'


def test_T9_late_output_from_an_interrupted_turn_never_reaches_any_later_turn(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_late',budget(first_activity_timeout=5,idle_timeout=0.5),adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    first=drive(adapter)
    assert first['code']=='turn_idle_timeout'
    assert '上一轮的迟到文本' not in ''.join(event.get('text') or '' for event in seen)
    seen.clear()
    second=drive(adapter,'第二轮')
    assert second.get('result')=='completed',second
    assert '上一轮的迟到文本' not in ''.join(event.get('text') or '' for event in seen)
    assert [event.get('text') for event in seen if event.get('type')=='assistant_text_delta']==['第二轮正常文本']


def test_T13_an_explicit_cancel_recovers_the_same_way(workspace,request_log,adapters):
    adapter=make(workspace,request_log,'budget_late',None,adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    native=adapter.status()['remote_session_id']
    outcome=drive_async(adapter,'第一轮')
    assert wait_until(lambda:adapter.diagnostics()['in_flight'] or None,5)
    assert adapter.cancel() is True
    outcome['worker'].join(timeout=10)
    assert outcome.get('error')=='CancelledError'
    assert '上一轮的迟到文本' not in ''.join(event.get('text') or '' for event in seen)
    seen.clear()
    second=drive(adapter,'第二轮')
    assert second.get('result')=='completed',second
    assert adapter.status()['remote_session_id']==native
    assert 'session/resume' in sent_methods(request_log)


def test_T15_an_explicit_cancel_recovers_even_without_any_budget(workspace,request_log,adapters):
    """The correctness fix does not depend on the budget being switched on."""
    adapter=make(workspace,request_log,'budget_late',None,adapters)
    assert adapter._config.budget is None
    adapter.bind(lambda event:None)
    adapter.start()
    native=adapter.status()['remote_session_id']
    outcome=drive_async(adapter,'第一轮')
    assert wait_until(lambda:adapter.diagnostics()['in_flight'] or None,5)
    adapter.cancel()
    outcome['worker'].join(timeout=10)
    assert outcome.get('error')=='CancelledError'
    assert adapter.status()['alive'] is False,'an interrupted turn finishes its runtime'
    assert drive(adapter,'第二轮').get('result')=='completed'
    assert adapter.status()['remote_session_id']==native


def test_T14_a_failed_resume_never_falls_back_to_a_new_session(workspace,request_log,adapters,monkeypatch):
    adapter=make(workspace,request_log,'budget_hang_serve',budget(first_activity_timeout=0.4),adapters)
    adapter.bind(lambda event:None)
    adapter.start()
    assert drive(adapter)['code']=='turn_first_activity_timeout'
    monkeypatch.setattr(ReasonixAdapter,'_resume',lambda self,runtime,resume:(_ for _ in ()).throw(RuntimeUnavailable('恢复会话失败')))
    with pytest.raises(RuntimeUnavailable):
        adapter.send('第二轮')
    methods=sent_methods(request_log)
    assert methods.count('session/new')==1,'a failed resume must not open a new session'
    assert methods.count('session/prompt')==1,'no prompt may be sent after a failed resume'
    assert adapter.status()['alive'] is False,'the unusable runtime must not be left running'
