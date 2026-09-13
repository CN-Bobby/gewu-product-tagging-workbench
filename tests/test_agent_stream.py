"""M2B: agent event SSE stream. Offline, no web server needed for the streaming behaviour.

Cursor rules and the wire format are exercised through pure helpers; the generator is driven
directly with an injected sleep so heartbeat and polling never wait for real time. Only the
non-streaming HTTP outcomes (404/400) go through the real app.
"""
import asyncio
import json
import re
import threading
import pytest
from backend.agent import events,stream
from fastapi.testclient import TestClient

FAST={'poll_seconds':0.01,'heartbeat_seconds':0.03}


def event(type,**extra):return {'type':type,'timestamp':'2026-09-13T00:00:00+00:00',**extra}


def new_session():
    return events.open_session(dataset_id='ds-1',provider='deepseek',model='deepseek/deepseek-v4-flash')


def seed(session_id,count,type='request_started'):
    for index in range(count):events.append(session_id,event(type,ident=index))
    return events.latest_sequence(session_id)


def ids_in(frames):
    return [int(match.group(1)) for frame in frames if (match:=re.match(r'id: (\d+)\n',frame))]


def drive(session_id,cursor=0,frames=1,**options):
    """Collect up to `frames` frames and then close the generator like a disconnecting client."""
    async def run():
        collected=[];slept=[]
        async def sleep(seconds):slept.append(seconds)
        generator=stream.event_stream(session_id,cursor,sleep=sleep,**{**FAST,**options})
        try:
            async for frame in generator:
                collected.append(frame)
                if len(collected)>=frames:break
        finally:
            await generator.aclose()
        return collected,slept
    return asyncio.run(run())


@pytest.fixture
def client():
    from backend.app import app
    with TestClient(app) as test_client:yield test_client


# --- pure helpers ---------------------------------------------------------

def test_1_a_missing_session_is_404(client):
    assert client.get('/api/agent/sessions/does-not-exist/events').status_code==404


def test_2_and_3_cursor_defaults_and_rejects_junk():
    assert stream.resolve_cursor()==0 and stream.resolve_cursor(None,None)==0
    assert stream.resolve_cursor('','')==0
    assert stream.resolve_cursor('0')==0 and stream.resolve_cursor('  7 ')==7
    for bad in ('abc','-1','1.5','1e3','٣'):
        with pytest.raises(ValueError,match='非负整数'):stream.resolve_cursor(bad)


def test_2_invalid_cursor_is_400_not_a_silent_zero(client):
    session=new_session()
    response=client.get('/api/agent/sessions/'+session['id']+'/events?after_sequence=abc')
    assert response.status_code==400 and '非负整数' in response.json()['detail']


def test_4_after_sequence_returns_the_remaining_events():
    session=new_session();seed(session['id'],5)
    frames,_=drive(session['id'],cursor=2,frames=3)
    assert ids_in(frames)==[3,4,5]


def test_5_to_9_frames_are_ordered_and_shaped():
    session=new_session();seed(session['id'],3)
    frames,_=drive(session['id'],frames=3)
    assert ids_in(frames)==[1,2,3]
    for index,frame in enumerate(frames):
        lines=frame.split('\n')
        assert lines[0]=='id: '+str(index+1) and lines[1]=='event: agent_event'
        assert lines[2].startswith('data: ') and frame.endswith('\n\n')
        payload=json.loads(lines[2][len('data: '):])
        assert payload['sequence']==index+1 and payload['session_id']==session['id']
        assert payload['event_type']=='request_started' and payload['source']=='runtime'
    assert frames[0].count('\n')==4


def test_8_and_9_payload_round_trips_losslessly_with_chinese():
    session=new_session()
    events.append(session['id'],event('protocol_error',kind='invalid_json',raw='oops',
        nested={'list':[1,{'deep':'中文'}],'flag':True,'none':None},number=1.5))
    frames,_=drive(session['id'],frames=1)
    payload=json.loads(frames[0].split('\n')[2][len('data: '):])
    assert payload['payload']['nested']=={'list':[1,{'deep':'中文'}],'flag':True,'none':None}
    assert payload['payload']['number']==1.5 and payload['payload']['raw']=='oops'
    assert '中文' in frames[0]


def test_10_to_15_reconnect_resumes_without_duplicates_or_loss():
    session=new_session();seed(session['id'],5)
    first,_=drive(session['id'],frames=3)
    assert ids_in(first)==[1,2,3]
    by_query,_=drive(session['id'],cursor=stream.resolve_cursor('3',None),frames=10)
    by_header,_=drive(session['id'],cursor=stream.resolve_cursor(None,'3'),frames=10)
    assert ids_in(by_query)==[4,5] and ids_in(by_header)==[4,5]
    assert stream.resolve_cursor('2','5')==2                  # query wins over the header
    combined=ids_in(first)+ids_in(by_query)
    assert combined==[1,2,3,4,5] and len(set(combined))==5     # no duplicate, no gap


def test_16_to_19_backlog_is_drained_without_sleeping_between_batches():
    session=new_session();seed(session['id'],250)
    frames,slept=drive(session['id'],frames=250)
    assert ids_in(frames)==list(range(1,251))
    assert len(set(ids_in(frames)))==250
    assert slept==[]                                            # no artificial wait while catching up


def test_16_backlog_larger_than_one_batch_needs_several_reads(monkeypatch):
    session=new_session();seed(session['id'],250)
    reads=[]
    original=events.read
    monkeypatch.setattr(events,'read',lambda *args,**kwargs:(reads.append(kwargs.get('limit')),original(*args,**kwargs))[1])
    frames,_=drive(session['id'],frames=250,batch_limit=100)
    assert ids_in(frames)==list(range(1,251))
    assert reads[:3]==[100,100,100]                             # drains in explicit batches


def test_20_to_22_idle_stream_emits_a_heartbeat_that_changes_nothing():
    session=new_session()
    frames,_=drive(session['id'],frames=1)
    assert frames[0]==': heartbeat\n\n'
    assert 'id:' not in frames[0] and 'event:' not in frames[0]

    async def run():
        async def sleep(seconds):pass
        generator=stream.event_stream(session['id'],0,sleep=sleep,**FAST)
        heartbeat=await generator.__anext__()                    # idle: heartbeat
        events.append(session['id'],event('process_started'))     # now sequence 1 exists
        first=await generator.__anext__()
        await generator.aclose()
        return heartbeat,first
    heartbeat,first=asyncio.run(run())
    assert heartbeat==stream.heartbeat_frame() and first.startswith('id: 1\n')


def test_23_disconnect_ends_the_generator_cleanly():
    session=new_session();seed(session['id'],2)
    async def run():
        generator=stream.event_stream(session['id'],0,sleep=lambda seconds:asyncio.sleep(0),**FAST)
        assert (await generator.__anext__()).startswith('id: 1\n')
        await generator.aclose()
        with pytest.raises(StopAsyncIteration):await generator.__anext__()
        return True
    assert asyncio.run(run())


def test_24_stream_starts_no_thread():
    session=new_session();seed(session['id'],1)
    before={thread.name for thread in threading.enumerate()}
    drive(session['id'],frames=1)
    assert not {thread.name for thread in threading.enumerate()}-before


def test_25_disconnect_stops_reading_the_store(monkeypatch):
    session=new_session();seed(session['id'],1)
    reads=[]
    original=events.read
    monkeypatch.setattr(events,'read',lambda *args,**kwargs:(reads.append(1),original(*args,**kwargs))[1])
    drive(session['id'],frames=1)
    settled=len(reads)
    assert settled>=1
    asyncio.run(asyncio.sleep(0))
    assert len(reads)==settled


def test_26_the_draft_stream_is_still_registered_next_to_the_agent_stream():
    from backend.app import app
    paths={getattr(route,'path','') for route in app.routes}
    assert '/api/drafts/{did}/events' in paths
    assert '/api/agent/sessions/{session_id}/events' in paths


def test_27_stream_headers_and_media_type():
    """Inspect the response object instead of consuming an endless body."""
    from backend.app import agent_events
    session=new_session()
    class FakeRequest:
        headers={}
        async def is_disconnected(self):return False
    response=asyncio.run(agent_events(session['id'],FakeRequest()))
    assert response.media_type=='text/event-stream'
    assert response.headers['cache-control']=='no-cache'
    assert response.headers['x-accel-buffering']=='no'
