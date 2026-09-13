"""P0 F02: a turn must be bounded in time, and the reported error must be the real blocker."""
import threading
import time
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend import harness
from backend.db import Session,BatchJob,require,uid
from backend.harness import ACP,HarnessError

MCP_TIMEOUT_LINE='2026/09/13 18:27:21 WARN plugin: MCP call timed out server=product_workbench method=tools/call tool=ask_business_question timeout=5m0s'


def fake_client(**overrides):
    instance=object.__new__(ACP)
    instance.question_count=0;instance.pending_questions={};instance.question_lock=threading.Lock()
    instance.current_issue=None;instance.last_tool_failure=None;instance.user_cancelled=False;instance.ask_seen=False
    instance.diagnostics=[];instance.model='provider/model';instance.session_id='session-test'
    instance.proc=SimpleNamespace(poll=lambda:None)
    instance.text=[];instance.tool_calls={};instance.workbench_completed=0;instance.workbench_failed=0
    instance.issues=[];instance.turn_limit=None
    instance.cancelled=threading.Event()
    instance._send=lambda msg:None
    def cancel():
        instance.user_cancelled=True;instance.cancelled.set()
    instance.cancel=cancel
    for key,value in overrides.items():setattr(instance,key,value)
    return instance


def blocking_rpc(client,result=None):
    def rpc(*args,**kwargs):
        client.cancelled.wait(10)
        return result if result is not None else {'stopReason':'end_turn'}
    return rpc


def test_total_turn_limit_raises_turn_timeout(monkeypatch):
    monkeypatch.setattr(harness,'TURN_TOTAL_SECONDS',0.2)
    client=fake_client();client.rpc=blocking_rpc(client)
    with pytest.raises(HarnessError) as error:client.prompt('读一下工作表')
    assert error.value.code=='turn_timeout'


def test_first_event_limit_fires_before_silence(monkeypatch):
    monkeypatch.setattr(harness,'TURN_FIRST_EVENT_SECONDS',0.15)
    monkeypatch.setattr(harness,'TURN_SILENCE_SECONDS',30)
    monkeypatch.setattr(harness,'TURN_TOTAL_SECONDS',30)
    client=fake_client();client.rpc=blocking_rpc(client)
    with pytest.raises(HarnessError) as error:client.prompt('读一下工作表')
    assert error.value.code=='turn_no_first_event'


def test_silence_limit_reports_a_stalled_turn(monkeypatch):
    monkeypatch.setattr(harness,'TURN_FIRST_EVENT_SECONDS',30)
    monkeypatch.setattr(harness,'TURN_SILENCE_SECONDS',0.15)
    monkeypatch.setattr(harness,'TURN_TOTAL_SECONDS',30)
    client=fake_client()
    def rpc(*args,**kwargs):
        client.first_event_at=time.monotonic();client.last_activity=time.monotonic()
        client.cancelled.wait(10)
        return {'stopReason':'end_turn'}
    client.rpc=rpc
    with pytest.raises(HarnessError) as error:client.prompt('读一下工作表')
    assert error.value.code=='turn_stalled'


def test_a_later_mcp_timeout_outranks_an_earlier_recoverable_denial(monkeypatch):
    client=fake_client()
    def rpc(*args,**kwargs):
        client.issues.append({'code':'tool_permission_denied','message':'Reasonix 尝试调用工作台未开放的工具“run_skill”，已阻止。','at':1.0,'weight':1})
        client.current_issue=client.issues[-1]
        client.diagnostics=[MCP_TIMEOUT_LINE]
        client.workbench_completed=28
        return {'stopReason':'end_turn'}
    client.rpc=rpc
    with pytest.raises(HarnessError) as error:client.prompt('判断上衣样式')
    assert error.value.code=='tool_timeout'
    assert error.value.technical['diagnostics']


def test_a_recovered_denial_does_not_fail_the_turn(monkeypatch):
    client=fake_client()
    def rpc(*args,**kwargs):
        client.issues.append({'code':'tool_permission_denied','message':'Reasonix 尝试调用工作台未开放的工具“run_skill”，已阻止。','at':1.0,'weight':1})
        client.current_issue=client.issues[-1]
        client._note_tool_result('completed',True)
        client.text.append('当前判断：已读取工作表。')
        return {'stopReason':'end_turn'}
    client.rpc=rpc
    assert client.prompt('读一下工作表')=='当前判断：已读取工作表。'
    assert client.issues==[]


def test_a_lone_denial_is_still_reported(monkeypatch):
    client=fake_client()
    def rpc(*args,**kwargs):
        client.issues.append({'code':'tool_permission_denied','message':'Reasonix 尝试调用工作台未开放的工具“bash”，已阻止。','at':1.0,'weight':1})
        client.current_issue=client.issues[-1]
        return {'stopReason':'end_turn'}
    client.rpc=rpc
    with pytest.raises(HarnessError) as error:client.prompt('读一下工作表')
    assert error.value.code=='tool_permission_denied' and 'bash' in str(error.value)


def test_tool_label_uses_the_real_capability_name():
    assert '工作表' in harness._tool_label('mcp__product_workbench__read_worksheet')
    assert harness._tool_label('mcp__product_workbench__unknown_thing')=='正在读取工作台信息'
    assert harness._tool_label(None)=='正在读取工作台信息'


def test_generated_config_bounds_the_question_bridge_and_denies_run_skill(dataset):
    did=dataset['id']
    path=harness.runtime_dir(did,'chat-explicit',role='coordinator')
    config=(path/'reasonix.toml').read_text(encoding='utf-8')
    assert 'call_timeout_seconds = 300' in config
    assert '"ask_business_question" = 30' in config
    assert 'run_skill' in config
    prompt=(path/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'mcp__product_workbench__route_tagging_skill' in prompt
    assert '先调用 route_tagging_skill' not in prompt
