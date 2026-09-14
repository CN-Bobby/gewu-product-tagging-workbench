"""M3A.5: turn lifecycle, cancellation and delta coalescing. FakeBackend only, offline."""
import asyncio
import json
import re
import threading
import time
from pathlib import Path
import pytest
from backend.agent import events,stream
from backend.agent.backend import CancelledError,RuntimeUnavailable,TurnError
from backend.agent.bridge import (AgentBridge,BackendSendError,InvalidSessionState,
    MAX_DELTA_CHARS,SessionBusy,SessionNotFound)
from fake_backend import FakeBackend

AGENT_DIR=Path(__file__).resolve().parents[1]/'backend'/'agent'


def make(mode='normal',**kwargs):
    backends=[]
    def factory():
        backend=FakeBackend(mode=mode,**kwargs);backends.append(backend);return backend
    return AgentBridge(factory,dataset_id='ds-1'),backends


def active(mode='normal',**kwargs):
    bridge,backends=make(mode,**kwargs)
    sid=bridge.create_session(provider='deepseek',model='deepseek/deepseek-v4-flash')['id']
    bridge.start(sid)
    return bridge,backends,sid


def types_of(sid,limit=5000):
    return [row['event_type'] for row in events.read(sid,limit=limit)]


def payloads_of(sid,kind):
    return [row['payload'] for row in events.read(sid,limit=5000) if row['event_type']==kind]


def send_in_thread(bridge,sid,text):
    """Run one send off-thread so cancel() can be called while it is in flight."""
    outcome={}
    def work():
        try:outcome['result']=bridge.send(sid,text)
        except BaseException as error:outcome['error']=error
    thread=threading.Thread(target=work);thread.start()
    return thread,outcome


# --- turn failure: non fatal ---------------------------------------------

def test_1_to_3_a_turn_error_is_recorded_and_the_session_survives():
    bridge,backends,sid=active('turn_fail')
    with pytest.raises(TurnError):bridge.send(sid,'你好')
    names=types_of(sid)
    assert names[-1]=='message_failed' and 'session_failed' not in names
    assert payloads_of(sid,'message_failed')[0]['fatal'] is False
    assert bridge.get_session(sid)['status']=='active'
    backends[0].mode='normal'
    assert bridge.send(sid,'继续')['event_type']=='message_completed'


def test_an_undeclared_backend_exception_is_also_non_fatal():
    bridge,backends,sid=active('crash_send')
    with pytest.raises(BackendSendError):bridge.send(sid,'你好')
    assert payloads_of(sid,'message_failed')[0]['fatal'] is False
    assert bridge.get_session(sid)['status']=='active'


# --- turn failure: fatal -------------------------------------------------

def test_4_to_6_a_fatal_failure_records_the_turn_and_fails_the_session():
    bridge,backends,sid=active('fatal_fail')
    with pytest.raises(RuntimeUnavailable):bridge.send(sid,'你好')
    assert types_of(sid)[-2:]==['message_failed','session_failed']
    assert payloads_of(sid,'message_failed')[0]['fatal'] is True
    assert bridge.get_session(sid)['status']=='failed'
    with pytest.raises(InvalidSessionState):bridge.send(sid,'再来')


# --- cancellation --------------------------------------------------------

def test_7_to_11_a_slow_turn_can_be_cancelled_and_the_session_continues():
    bridge,backends,sid=active('slow')
    thread,outcome=send_in_thread(bridge,sid,'慢')
    assert backends[0].entered.wait(5)                       # in flight, by handshake not by sleep
    requested=bridge.cancel(sid)
    assert requested=={'session_id':sid,'cancelled':True,'message_id':requested['message_id']}
    thread.join(5)
    assert not thread.is_alive() and isinstance(outcome['error'],CancelledError)
    names=types_of(sid)
    assert 'message_cancel_requested' in names and names[-1]=='message_cancelled'
    assert bridge.get_session(sid)['status']=='active'
    assert backends[0].cancel_calls==1
    backends[0].mode='normal'
    assert bridge.send(sid,'继续')['event_type']=='message_completed'


def test_12_cancel_on_an_idle_session_is_explicit_and_idempotent():
    bridge,backends,sid=active()
    before=len(types_of(sid))
    for _ in range(2):
        assert bridge.cancel(sid)=={'session_id':sid,'cancelled':False,'status':'active'}
    assert len(types_of(sid))==before and backends[0].cancel_calls==0


def test_13_cancel_and_stop_are_separate():
    bridge,backends,sid=active()
    bridge.cancel(sid)                                        # nothing running
    assert backends[0].stop_calls==0 and bridge.get_session(sid)['status']=='active'
    bridge.stop(sid)
    assert backends[0].stop_calls==1
    assert bridge.cancel(sid)['cancelled'] is False           # cancel after stop is harmless
    assert bridge.get_session(sid)['status']=='stopped'
    with pytest.raises(InvalidSessionState):bridge.send(sid,'继续')


def test_cancel_is_not_blocked_by_a_running_turn():
    bridge,backends,sid=active('slow')
    thread,outcome=send_in_thread(bridge,sid,'慢')
    assert backends[0].entered.wait(5)
    with pytest.raises(SessionBusy):bridge.send(sid,'第二个')  # a second turn is refused
    assert bridge.cancel(sid)['cancelled'] is True             # cancel gets through regardless
    thread.join(5)
    assert bridge.status(sid)['status']=='active'              # status is readable while busy


# --- delta coalescing ----------------------------------------------------

def test_17_and_18_a_thousand_deltas_become_few_chunks_and_keep_the_exact_text():
    reply=tuple('字' for _ in range(1000))
    bridge,backends,sid=active('normal',reply=reply)
    assert bridge.send(sid,'长回复')['event_type']=='message_completed'
    chunks=payloads_of(sid,'assistant_text_delta')
    assert len(chunks)<1000 and len(chunks)==5                 # MAX_DELTA_CHARS=200
    assert ''.join(chunk['text'] for chunk in chunks)=='字'*1000
    assert all(len(chunk['text'])<=MAX_DELTA_CHARS for chunk in chunks)
    assert [chunk['merged'] for chunk in chunks]==[200,200,200,200,200]
    assert all(chunk['message_id'] for chunk in chunks)


def test_19_the_turn_settled_flushes_what_is_left_and_keeps_chinese_intact():
    """A five-character reply never crosses the size or time threshold on its own, so the turn
    event is the only thing that gets it into the log."""
    bridge,backends,sid=active('normal',reply=('你好，世界',))
    bridge.send(sid,'短回复')
    chunks=payloads_of(sid,'assistant_text_delta')
    assert [chunk['text'] for chunk in chunks]==['你好，世界']
    assert chunks[0]['flushed_by']=='completed' and chunks[0]['merged']==1
    assert types_of(sid)[-3:]==['assistant_text_delta','turn_settled','message_completed']


def test_20_a_failed_turn_flushes_what_is_left():
    bridge,backends,sid=active('mid_fail')
    with pytest.raises(TurnError):bridge.send(sid,'会失败')
    chunks=payloads_of(sid,'assistant_text_delta')
    assert [chunk['text'] for chunk in chunks]==['半']
    assert chunks[0]['flushed_by']=='failed'
    assert types_of(sid)[-2:]==['assistant_text_delta','message_failed']


def test_21_a_cancelled_turn_flushes_what_is_left():
    bridge,backends,sid=active('slow')
    thread,outcome=send_in_thread(bridge,sid,'会取消')
    assert backends[0].entered.wait(5)
    bridge.cancel(sid)
    thread.join(5)
    chunks=payloads_of(sid,'assistant_text_delta')
    assert [chunk['text'] for chunk in chunks]==['半'] and chunks[0]['flushed_by']=='cancelled'


def test_22_and_23_tool_events_are_not_coalesced_and_do_not_touch_the_text_buffer():
    bridge,backends,sid=active('normal',tool_events=('tool_call_started','tool_call_settled'),reply=('甲','乙'))
    bridge.send(sid,'带工具')
    rows=[(row['event_type'],row['source']) for row in events.read(sid)]
    assert rows[-5:]==[('tool_call_started','tool'),('tool_call_settled','tool'),
                       ('assistant_text_delta','assistant'),('turn_settled','assistant'),
                       ('message_completed','bridge')]
    assert [payload['text'] for payload in payloads_of(sid,'assistant_text_delta')]==['甲乙']


def test_24_the_sse_stream_reads_the_coalesced_delta_in_sequence():
    bridge,backends,sid=active('normal',reply=tuple('字' for _ in range(1000)))
    bridge.send(sid,'长回复')
    total=len(types_of(sid))
    async def collect():
        frames=[]
        generator=stream.event_stream(sid,0,sleep=lambda seconds:asyncio.sleep(0),poll_seconds=0.01,heartbeat_seconds=5)
        try:
            async for frame in generator:
                frames.append(frame)
                if len(frames)>=total:break
        finally:
            await generator.aclose()
        return frames
    frames=asyncio.run(collect())
    assert [int(re.match(r'id: (\d+)\n',frame).group(1)) for frame in frames]==list(range(1,total+1))
    deltas=[json.loads(frame.split('\n')[2][len('data: '):])['payload'] for frame in frames
            if json.loads(frame.split('\n')[2][len('data: '):])['event_type']=='assistant_text_delta']
    assert len(deltas)==5 and ''.join(delta['text'] for delta in deltas)=='字'*1000


# --- session timestamps --------------------------------------------------

def test_25_to_27_session_timestamps_follow_their_meaning():
    bridge,backends=make()
    created=bridge.create_session()
    assert created['created_at'] and created['started_at'] is None and created['stopped_at'] is None
    started=bridge.start(created['id'])
    assert started['started_at'] and started['started_at']>=started['created_at']
    assert started['stopped_at'] is None
    stopped=bridge.stop(created['id'])
    assert stopped['stopped_at'] and stopped['stopped_at']>=stopped['started_at']

    fatal,_,sid=active('fatal_fail')
    with pytest.raises(RuntimeUnavailable):fatal.send(sid,'失败')
    assert fatal.get_session(sid)['stopped_at']


# --- boundary ------------------------------------------------------------

def test_28_to_30_the_bridge_still_stays_inside_its_boundary():
    source=(AGENT_DIR/'bridge.py').read_text(encoding='utf-8')
    imports=re.findall(r'^\s*(?:from|import)\s+.*$',source,re.M)
    for name in ('runtime','harness','conversation','draft','business_questions','presentation','mcp_server','db'):
        assert not [line for line in imports if name in line],name
    for token in ('reasonix','ACP','conversation','draft','presentation','business_question','Runtime(','subprocess'):
        assert token not in source,token


# --- M3B-4C: the turn outcome reaches the message outcome -----------------

def test_a_cancelled_turn_records_the_outcome_then_cancels_the_message():
    bridge,backends,sid=active('cancelled_turn')
    with pytest.raises(CancelledError):bridge.send(sid,'取消')
    assert types_of(sid)[-3:]==['assistant_text_delta','turn_settled','message_cancelled']
    assert bridge.status(sid)['status']=='active'
    assert bridge.get_session(sid)['status']=='active'


def test_a_cancelled_turn_keeps_the_wire_causal_order_in_one_log():
    """The text arrives before the tool events on the wire, so it must be stored before them too:
    a buffered short reply may not be overtaken by a later event."""
    bridge,backends,sid=active('cancelled_turn',tool_events=('tool_call_started','tool_call_settled'))
    with pytest.raises(CancelledError):bridge.send(sid,'带工具的取消')
    rows=[row['event_type'] for row in events.read(sid)]
    assert rows[-5:]==['assistant_text_delta','tool_call_started','tool_call_settled',
                       'turn_settled','message_cancelled']
    assert payloads_of(sid,'assistant_text_delta')[0]['text']=='半'


def test_a_short_delta_is_flushed_before_a_following_tool_event():
    """Half a character never crosses MAX_DELTA_CHARS, so a flush at the tool event is the only
    thing that keeps the stored order causal; without it the tool would be logged first."""
    bridge,backends,sid=active('cancelled_turn',tool_events=('tool_call_started',))
    with pytest.raises(CancelledError):bridge.send(sid,'短')
    assert len('半')<MAX_DELTA_CHARS
    assert types_of(sid)[-4:]==['assistant_text_delta','tool_call_started',
                                'turn_settled','message_cancelled']
    chunks=payloads_of(sid,'assistant_text_delta')
    assert [chunk['text'] for chunk in chunks]==['半'] and chunks[0]['flushed_by']=='ordered'


def test_an_end_turn_still_completes_the_message():
    bridge,backends,sid=active()
    assert bridge.send(sid,'正常')['event_type']=='message_completed'
    assert types_of(sid)[-3:]==['assistant_text_delta','turn_settled','message_completed']


# --- M3B-5: interaction events obey the same ordering rule -----------------

def test_a_question_request_does_not_overtake_the_buffered_delta():
    """Any non-delta event flushes the text first, so a short reply cannot be overtaken by the
    interaction request that came after it on the wire."""
    bridge,backends,sid=active('ask_question')
    bridge.send(sid,'提问')
    rows=[(row['event_type'],row['source']) for row in events.read(sid)]
    assert rows[-3:]==[('assistant_text_delta','assistant'),('question_requested','system'),
                       ('message_completed','bridge')]
    assert payloads_of(sid,'assistant_text_delta')[0]['text']=='半'


# --- M3B-5: the application answers through the bridge ---------------------

def test_an_application_answers_a_question_through_the_bridge():
    """Application -> bridge -> backend: the caller uses only the neutral interaction id and
    never learns how the backend addresses its peer."""
    bridge,backends,sid=active('ask_interaction')
    bridge.send(sid,'提问')
    assert types_of(sid)[-3:]==['message_started','question_requested','message_completed']
    assert bridge.answer_question(sid,'fake-interaction-1','a') is True
    assert types_of(sid)[-1]=='question_settled'
    assert types_of(sid).index('question_requested')<types_of(sid).index('question_settled')
    settled=payloads_of(sid,'question_settled')[-1]
    assert settled['interaction_id']=='fake-interaction-1'
    assert settled['outcome']=='selected' and settled['option_id']=='a'


def test_the_bridge_refuses_an_answer_the_backend_rejects():
    bridge,backends,sid=active('ask_interaction')
    bridge.send(sid,'提问')
    with pytest.raises(ValueError):bridge.answer_question(sid,'fake-interaction-1','z')
    with pytest.raises(ValueError):bridge.answer_question(sid,'unknown-id','a')
    assert bridge.cancel_question(sid,'fake-interaction-1') is True
    assert payloads_of(sid,'question_settled')[-1]['outcome']=='cancelled'


def test_an_answer_for_an_unknown_session_is_refused():
    bridge,backends,sid=active('ask_interaction')
    with pytest.raises(SessionNotFound):bridge.answer_question('no-such-session','i','a')
