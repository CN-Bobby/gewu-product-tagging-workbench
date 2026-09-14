"""Reasonix tool host lifecycle: which frames become events, and what those events may claim.

The frames are the real wire shapes; the contract under test is deliberately narrow. A tool event
reports what the Reasonix host said about one invocation, never what the MCP tool physically did —
a cancelled turn reports failed while the real tool keeps running, so the two are indistinguishable
here and no event is allowed to pretend otherwise.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from backend.agent import bridge,events as store,protocol,reasonix
from backend.agent.backend import RuntimeUnavailable

REPO=Path(__file__).resolve().parents[1]
STUB=Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py'
MODEL='deepseek/deepseek-v4-flash'
RUNTIME_KEY='runtime-key-1'

EXECUTION_CLAIM_KEYS=('execution_status','actual_status','physical_status','tool_failed',
                      'result','error','message','stop_reason')


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


def run(workspace,request_log,mode,opened):
    """Run one turn and return the tool events it produced, in order."""
    adapter=make(workspace,request_log,mode=mode,opened=opened)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    adapter.send('hi')
    return adapter,[event for event in seen if str(event.get('type')).startswith('tool_call')]


# --- which frames produce events ------------------------------------------

def test_a_real_mcp_call_reports_start_then_settle(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'normal',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_settled']
    assert tools[0]['host_status']=='pending'
    assert tools[1]['host_status']=='completed'
    assert [event['tool_call_id'] for event in tools]==['c1','c1']


def test_a_discovery_call_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_discovery',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'capability_discovery':1}


def test_an_internal_tool_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_internal',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'non_capability_tool':1}


def test_an_other_capability_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'prompt_extra',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'other_capability':1}


# --- what a settled event may claim ---------------------------------------

def test_a_host_failed_terminal_settles_as_failed(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_failed_context_cancelled',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_settled']
    assert tools[-1]['host_status']=='failed'


def test_a_handler_failure_and_a_cancellation_are_indistinguishable_here(workspace,request_log,adapters):
    """Both real failed shapes must produce the same contract: the host reported failed, and
    nothing more. Telling them apart is not this layer's job and must never be attempted by
    reading the text."""
    _,cancelled=run(workspace,request_log,'tool_failed_context_cancelled',adapters)
    _,raised=run(workspace,request_log,'tool_handler_failed',adapters)
    assert [event['type'] for event in cancelled]==[event['type'] for event in raised]
    assert cancelled[-1]['host_status']==raised[-1]['host_status']=='failed'
    assert sorted(cancelled[-1])==sorted(raised[-1])


def test_no_tool_event_carries_an_execution_claim(workspace,request_log,adapters):
    """Nothing about inputs, outputs, text or an inferred outcome may appear: whatever is written
    here is persisted, and the adapter has no channel to observe the tool itself."""
    for mode in ('normal','tool_failed_context_cancelled','tool_handler_failed','tool_multi'):
        _,tools=run(workspace,request_log,mode,adapters)
        for event in tools:
            assert set(event)<={'type','tool_call_id','server','tool','capability_id','host_status'},(mode,event)
            for key in EXECUTION_CLAIM_KEYS:
                assert key not in event,(mode,key)


def test_a_settled_event_carries_identity_and_host_status_only(workspace,request_log,adapters):
    _,tools=run(workspace,request_log,'normal',adapters)
    settled=tools[-1]
    assert settled=={'type':'tool_call_settled','tool_call_id':'c1','server':'product_workbench',
                     'tool':'read_worksheet','capability_id':'mcp-tool:product_workbench/read_worksheet',
                     'host_status':'completed'}


# --- exactly once ----------------------------------------------------------

def test_a_repeated_start_frame_reports_one_start(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_dup_start',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_settled']


def test_a_repeated_terminal_reports_one_settle(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_dup_settle',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_settled']


def test_a_later_contradicting_terminal_is_not_a_second_settle(workspace,request_log,adapters):
    """The wire has only been verified to go pending -> completed|failed, so the first verified
    terminal closes the lifecycle and a later reversal stays unreported."""
    adapter,tools=run(workspace,request_log,'tool_flip_status',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_settled']
    assert tools[-1]['host_status']=='completed'


# --- things that must stay unreported --------------------------------------

def test_an_update_without_a_known_start_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_orphan_update',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'unknown':1}


def test_a_start_that_is_not_pending_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_non_pending_start',adapters)
    assert tools==[]


def test_a_start_without_a_status_reports_nothing(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_missing_status',adapters)
    assert tools==[]


def test_a_start_without_a_status_cannot_be_closed_later(workspace,request_log,adapters):
    """A classified call is not a reported one. Without an outward start there is no lifecycle to
    close, so the later terminal must stay unreported."""
    adapter,tools=run(workspace,request_log,'tool_missing_status_then_terminal',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'mcp_tool_call':1}


def test_a_non_pending_start_cannot_be_closed_later(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_unknown_start_then_terminal',adapters)
    assert tools==[]
    assert adapter.diagnostics()['tool_classes']=={'mcp_tool_call':1}


def test_an_unverified_terminal_status_never_settles(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_unknown_status',adapters)
    assert [event['type'] for event in tools]==['tool_call_started']
    assert tools[0]['host_status']=='pending'


# --- identity and ordering -------------------------------------------------

def test_two_calls_report_independently_and_do_not_cross(workspace,request_log,adapters):
    adapter,tools=run(workspace,request_log,'tool_multi',adapters)
    assert [event['type'] for event in tools]==['tool_call_started','tool_call_started',
                                                'tool_call_settled','tool_call_settled']
    assert [event['tool_call_id'] for event in tools]==['call_A','call_B','call_B','call_A']
    settled={event['tool_call_id']:event['host_status'] for event in tools if event['type']=='tool_call_settled'}
    assert settled=={'call_A':'failed','call_B':'completed'}
    assert adapter.diagnostics()['tool_classes']=={'mcp_tool_call':2}


def test_the_outward_order_follows_the_wire_order(workspace,request_log,adapters):
    """No presentation-layer reordering: whatever order the frames arrived in is the order the
    events leave in, with the turn outcome last."""
    adapter=make(workspace,request_log,mode='tool_multi',opened=adapters)
    seen=[]
    adapter.bind(seen.append)
    adapter.start()
    adapter.send('hi')
    assert [event.get('type') for event in seen]==['tool_call_started','tool_call_started',
        'tool_call_settled','tool_call_settled','assistant_text_delta','turn_settled']
    assert [event['text'] for event in seen if event.get('type')=='assistant_text_delta']==['after-tools']


# --- the contract registration itself --------------------------------------

def test_the_lifecycle_event_names_are_registered_and_the_old_ones_are_gone():
    for name in ('tool_call_started','tool_call_settled'):
        assert name in bridge.OUTPUT_SOURCE_BY_TYPE,(name,'bridge')
        assert store.should_persist(name),(name,'persistence')
        assert bridge.OUTPUT_SOURCE_BY_TYPE[name]=='tool'
    for removed in ('tool_call_completed','tool_call_failed'):
        assert removed not in bridge.OUTPUT_SOURCE_BY_TYPE,(removed,'bridge')
        assert not store.should_persist(removed),(removed,'persistence')
