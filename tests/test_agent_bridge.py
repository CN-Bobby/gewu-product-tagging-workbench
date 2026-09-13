"""M3A: agent bridge core. Runs entirely against FakeBackend: no process, no network."""
import asyncio
import json
import re
import time
from pathlib import Path
import pytest
from backend.agent import events,stream
from backend.agent.bridge import (AgentBridge,BackendSendError,BackendStartError,BackendStopError,
    InvalidSessionState,SessionBusy,SessionNotFound)
from fake_backend import FakeBackend

AGENT_DIR=Path(__file__).resolve().parents[1]/'backend'/'agent'


def make(mode='normal',**kwargs):
    backends=[]
    def factory():
        backend=FakeBackend(mode=mode,**kwargs);backends.append(backend);return backend
    return AgentBridge(factory,dataset_id='ds-1'),backends


def active(mode='normal',**kwargs):
    bridge,backends=make(mode,**kwargs)
    session=bridge.create_session(provider='deepseek',model='deepseek/deepseek-v4-flash')
    bridge.start(session['id'])
    return bridge,backends,session['id']


def types_of(session_id,limit=100):
    return [row['event_type'] for row in events.read(session_id,limit=limit)]


# --- session lifecycle ----------------------------------------------------

def test_1_create_session_registers_a_created_session():
    bridge,backends=make()
    session=bridge.create_session(provider='deepseek',model='deepseek/deepseek-v4-flash')
    assert session['status']=='created' and session['dataset_id']=='ds-1'
    assert session['provider']=='deepseek' and session['remote_session_id'] is None
    assert types_of(session['id'])==['session_created']
    assert backends==[]                                  # no backend is built before start


def test_2_initial_status_is_readable():
    bridge,backends=make()
    session=bridge.create_session()
    assert bridge.get_session(session['id'])['status']=='created'
    status=bridge.status(session['id'])
    assert status['status']=='created' and status['backend'] is None


def test_3_and_4_start_walks_created_to_starting_to_active():
    bridge,backends=make()
    session=bridge.create_session()
    started=bridge.start(session['id'])
    assert started['status']=='active' and started['remote_session_id']=='fake-remote-1'
    assert backends[0].started
    rows=events.read(session['id'])
    assert [row['event_type'] for row in rows]==['session_created','session_starting','session_started']
    assert [row['sequence'] for row in rows]==[1,2,3]
    assert {row['source'] for row in rows}=={'bridge'}
    assert rows[2]['payload']['remote_session_id']=='fake-remote-1'


def test_5_to_9_a_turn_is_recorded_in_order_without_copying_the_prompt():
    bridge,backends,sid=active()
    result=bridge.send(sid,'你好')
    assert result['event_type']=='message_completed' and backends[0].sent==['你好']
    rows=events.read(sid)
    assert [(row['event_type'],row['source']) for row in rows]==[
        ('session_created','bridge'),('session_starting','bridge'),('session_started','bridge'),
        ('message_started','bridge'),('assistant_text_delta','assistant'),
        ('assistant_text_delta','assistant'),('assistant_message_completed','assistant'),
        ('message_completed','bridge')]
    payloads=[row['payload'] for row in rows]
    assert [p['text'] for p in payloads if p['type']=='assistant_text_delta']==['你','好']
    started=next(p for p in payloads if p['type']=='message_started')
    completed=next(p for p in payloads if p['type']=='message_completed')
    assert started['message_id']==completed['message_id'] and completed['seconds']>=0
    assert started['text_chars']==2 and '你好' not in json.dumps(started,ensure_ascii=False)


def test_10_and_11_a_failed_send_is_recorded_and_fails_the_session():
    bridge,backends=make('fail_send')
    sid=bridge.create_session()['id'];bridge.start(sid)
    with pytest.raises(BackendSendError):bridge.send(sid,'你好')
    assert bridge.get_session(sid)['status']=='failed'
    assert types_of(sid)[-2:]==['message_failed','session_failed']


def test_12_and_13_stop_records_stopping_then_stopped():
    bridge,backends,sid=active()
    result=bridge.stop(sid)
    assert result['status']=='stopped' and result['stopped_at']
    assert types_of(sid)[-2:]==['session_stopping','session_stopped']
    assert backends[0].stop_calls==1


def test_14_stop_is_idempotent():
    bridge,backends,sid=active()
    bridge.stop(sid)
    settled=len(events.read(sid,limit=100))
    again=bridge.stop(sid)
    assert again['status']=='stopped' and len(events.read(sid,limit=100))==settled
    assert backends[0].stop_calls==1


def test_14b_stop_after_failure_does_not_raise():
    bridge,backends=make('fail_send')
    sid=bridge.create_session()['id'];bridge.start(sid)
    with pytest.raises(BackendSendError):bridge.send(sid,'x')
    assert bridge.stop(sid)['status']=='failed'


def test_15_fail_start_marks_the_session_failed():
    bridge,backends=make('fail_start')
    sid=bridge.create_session()['id']
    with pytest.raises(BackendStartError):bridge.start(sid)
    assert bridge.get_session(sid)['status']=='failed'
    assert types_of(sid)[-2:]==['session_starting','session_failed']


def test_16_fail_stop_marks_the_session_failed():
    bridge,backends,sid=active('fail_stop')
    with pytest.raises(BackendStopError):bridge.stop(sid)
    assert bridge.get_session(sid)['status']=='failed'
    assert types_of(sid)[-2:]==['session_stopping','session_failed']


def test_illegal_transitions_and_missing_sessions_are_typed():
    bridge,backends=make()
    sid=bridge.create_session()['id']
    with pytest.raises(InvalidSessionState):bridge.send(sid,'你好')     # not active yet
    bridge.start(sid);bridge.stop(sid)
    with pytest.raises(InvalidSessionState):bridge.start(sid)          # stopped is terminal
    for call in (lambda:bridge.start('nope'),lambda:bridge.send('nope','x'),
                 lambda:bridge.stop('nope'),lambda:bridge.status('nope')):
        with pytest.raises(SessionNotFound):call()


# --- concurrency ----------------------------------------------------------

def test_17_a_second_turn_on_a_busy_session_is_refused():
    bridge,backends=make()
    sid=bridge.create_session()['id'];bridge.start(sid)
    backends[0].on_delta=lambda chunk:bridge.send(sid,'第二次')
    with pytest.raises(SessionBusy):bridge.send(sid,'第一次')
    backends[0].on_delta=None                                          # stop re-entering
    assert bridge.get_session(sid)['status']=='active'                 # 桥接错误不杀死会话
    assert bridge.send(sid,'第三次')['event_type']=='message_completed'  # 释放后可继续
    assert backends[0].sent==['第一次','第三次']


def test_18_two_sessions_are_isolated():
    bridge,backends=make()
    first=bridge.create_session(dataset_id='ds-a');second=bridge.create_session(dataset_id='ds-b')
    bridge.start(first['id']);bridge.start(second['id'])
    backends[0].on_delta=lambda chunk:bridge.send(first['id'],'再次')
    with pytest.raises(SessionBusy):bridge.send(first['id'],'第一次')
    assert bridge.send(second['id'],'你好')['event_type']=='message_completed'
    assert bridge.get_session(first['id'])['status']=='active'
    assert {row['dataset_id'] for row in events.read(first['id'])}=={'ds-a'}
    assert {row['dataset_id'] for row in events.read(second['id'])}=={'ds-b'}
    assert events.read(second['id'])[-1]['event_type']=='message_completed'
    assert 'message_completed' not in types_of(first['id'])


# --- ordering and wiring --------------------------------------------------

def test_19_backend_output_keeps_sequence_order():
    bridge,backends,sid=active()
    bridge.send(sid,'你好')
    rows=events.read(sid)
    assert [row['sequence'] for row in rows]==list(range(1,len(rows)+1))
    assert [row['sequence'] for row in rows if row['event_type'].startswith('assistant_')]==[5,6,7]


def test_20_the_sse_stream_carries_bridge_and_assistant_events_in_order():
    bridge,backends,sid=active()
    bridge.send(sid,'你好')
    async def collect():
        frames=[]
        generator=stream.event_stream(sid,0,sleep=lambda seconds:asyncio.sleep(0),poll_seconds=0.01,heartbeat_seconds=5)
        try:
            async for frame in generator:
                frames.append(frame)
                if len(frames)>=8:break
        finally:
            await generator.aclose()
        return frames
    frames=asyncio.run(collect())
    assert [int(re.match(r'id: (\d+)\n',frame).group(1)) for frame in frames]==[1,2,3,4,5,6,7,8]
    assert [json.loads(frame.split('\n')[2][len('data: '):])['event_type'] for frame in frames][3:]==[
        'message_started','assistant_text_delta','assistant_text_delta',
        'assistant_message_completed','message_completed']


def test_21_to_24_the_bridge_stays_inside_its_boundary():
    source=(AGENT_DIR/'bridge.py').read_text(encoding='utf-8')
    imports=re.findall(r'^\s*(?:from|import)\s+.*$',source,re.M)
    for name in ('runtime','harness','conversation','draft','business_questions','presentation','mcp_server','db'):
        assert not [line for line in imports if name in line],name
    for token in ('reasonix','ACP','conversation','draft','presentation','business_question','Runtime(','subprocess'):
        assert token not in source,token


@pytest.mark.parametrize('module',['runtime.py','protocol.py'])
def test_25_runtime_and_protocol_still_never_import_persistence(module):
    source=(AGENT_DIR/module).read_text(encoding='utf-8')
    imports=re.findall(r'^\s*(?:from|import)\s+.*$',source,re.M)
    assert not [line for line in imports if re.search(r'\bevents\b|\bdb\b|AgentEvent|AgentSession|bridge',line)],module


# --- extra behaviour ------------------------------------------------------

def test_a_contract_violation_is_recorded_but_does_not_kill_the_turn():
    bridge,backends,sid=active('noisy')
    assert bridge.send(sid,'你好')['event_type']=='message_completed'
    ignored=[row['payload'] for row in events.read(sid) if row['event_type']=='backend_event_ignored']
    assert len(ignored)==2
    assert any(item.get('backend_type')=='weird_private_event' for item in ignored)
    assert any(item.get('reason')=='missing_type' for item in ignored)
    assert bridge.get_session(sid)['status']=='active'


def test_a_slow_backend_still_records_one_ordered_turn():
    bridge,backends,sid=active('slow',delay=0.01)
    started=time.perf_counter();bridge.send(sid,'你好')
    assert time.perf_counter()-started>=0.02
    assert types_of(sid)[-4:]==['assistant_text_delta','assistant_text_delta',
                                'assistant_message_completed','message_completed']


def test_a_non_conforming_factory_result_fails_the_session():
    bridge=AgentBridge(lambda:object(),dataset_id='ds-1')
    sid=bridge.create_session()['id']
    with pytest.raises(BackendStartError):bridge.start(sid)
    assert bridge.get_session(sid)['status']=='failed'
