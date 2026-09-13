"""M2A: append-only agent event store and agent session registry. Offline, DB-backed."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from backend.agent import events
from backend.agent.runtime import Runtime

STUB=str(Path(__file__).resolve().parent/'fixtures'/'stub_acp_cli.py')
AGENT_DIR=Path(__file__).resolve().parents[1]/'backend'/'agent'


def event(type,**extra):return {'type':type,'timestamp':'2026-09-13T00:00:00+00:00',**extra}


def test_1_open_session_registers_an_agent_neutral_session():
    session=events.open_session(dataset_id='ds-1',provider='deepseek',model='deepseek/deepseek-v4-flash')
    assert session['id'] and session['status']=='active' and session['started_at'] and session['dataset_id']=='ds-1'
    stored=events.get_session(session['id'])
    assert stored['provider']=='deepseek' and stored['remote_session_id'] is None
    assert not {'cwd','instruction_hash','conversation_id','session_path','migration'}&set(stored)


def test_1b_session_updates_are_whitelisted_and_close_stamps_the_end():
    session=events.open_session()
    assert events.update_session(session['id'],status='suspended',remote_session_id='remote-1')['remote_session_id']=='remote-1'
    with pytest.raises(ValueError,match='不支持修改的字段'):
        events.update_session(session['id'],presentation={'x':1})
    closed=events.close_session(session['id'])
    assert closed['status']=='stopped' and closed['stopped_at']
    assert events.list_sessions()[0]['id']==session['id']


def test_2_append_a_single_event():
    session=events.open_session()
    stored=events.append(session['id'],event('process_started',pid=1234))
    assert stored['sequence']==1 and stored['event_type']=='process_started' and stored['source']=='runtime'
    assert stored['payload']['pid']==1234 and stored['payload']['type']=='process_started'
    assert stored['created_at'] and stored['session_id']==session['id']


def test_3_sequence_starts_at_one_and_increments():
    session=events.open_session()
    assert [events.append(session['id'],event('request_started',ident=index))['sequence'] for index in range(3)]==[1,2,3]


def test_4_one_hundred_events_keep_their_order():
    session=events.open_session()
    for index in range(100):events.append(session['id'],event('request_completed',ident=index))
    stored=events.read(session['id'],limit=1000)
    assert [row['sequence'] for row in stored]==list(range(1,101))
    assert [row['payload']['ident'] for row in stored]==list(range(100))
    assert events.latest_sequence(session['id'])==100 and events.count(session['id'])==100


def test_5_two_sessions_number_independently():
    first=events.open_session();second=events.open_session()
    events.append(first['id'],event('process_started'))
    events.append(second['id'],event('process_started'))
    events.append(second['id'],event('process_exited',returncode=0))
    assert [row['sequence'] for row in events.read(first['id'])]==[1]
    assert [row['sequence'] for row in events.read(second['id'])]==[1,2]


def test_6_payload_round_trips_without_loss():
    session=events.open_session()
    payload={'type':'protocol_error','timestamp':'t','kind':'invalid_json',
             'nested':{'list':[1,2,{'deep':'中文'}],'flag':True,'none':None},'number':1.5}
    events.append(session['id'],payload)
    assert events.read(session['id'])[0]['payload']==payload


def test_7_event_type_and_source_are_normalized():
    event_type,payload=events.normalize({'timestamp':'t'})
    assert event_type=='unknown' and payload['type']=='unknown'
    event_type,payload=events.normalize(event('protocol_error'),source='bridge')
    assert event_type=='protocol_error' and payload['timestamp']=='2026-09-13T00:00:00+00:00'
    with pytest.raises(ValueError,match='未知事件来源'):
        events.normalize(event('process_started'),source='nope')
    session=events.open_session()
    stored=events.append(session['id'],event('protocol_error'),source='bridge')
    assert stored['event_type']=='protocol_error' and stored['source']=='bridge'
    assert stored['payload']['type']=='protocol_error'
    # An untyped event is outside the policy, so it is filtered out instead of stored.
    assert events.append(session['id'],{'timestamp':'t'}) is None


def test_8_after_sequence_resumes_at_the_next_event():
    session=events.open_session()
    for index in range(5):events.append(session['id'],event('request_started',ident=index))
    assert [row['sequence'] for row in events.read(session['id'],after_sequence=2)]==[3,4,5]
    assert events.read(session['id'],after_sequence=5)==[]
    assert events.read(session['id'],after_sequence=152)==[]


def test_9_limit_is_honoured():
    session=events.open_session()
    for index in range(10):events.append(session['id'],event('request_started',ident=index))
    assert [row['sequence'] for row in events.read(session['id'],limit=2)]==[1,2]
    assert [row['sequence'] for row in events.tail(session['id'],limit=3)]==[8,9,10]


def test_10_and_11_protocol_error_and_request_timeout_are_stored():
    session=events.open_session()
    assert events.should_persist('protocol_error') and events.should_persist('request_timeout')
    events.append(session['id'],event('protocol_error',kind='invalid_json',raw='oops'))
    events.append(session['id'],event('request_timeout',method='session/prompt',timeout=0.3))
    stored=events.read(session['id'])
    assert [row['event_type'] for row in stored]==['protocol_error','request_timeout']
    assert stored[0]['payload']['raw']=='oops' and stored[1]['payload']['timeout']==0.3


def test_12_ordinary_stderr_and_bookkeeping_never_reach_the_log():
    session=events.open_session()
    for filtered in ('reader_closed','stderr_closed','reader_stopped','stderr_line'):
        assert events.append(session['id'],event(filtered,line='INFO something')) is None
    assert events.count(session['id'])==0


@pytest.mark.parametrize('module',['runtime.py','protocol.py'])
def test_13_and_14_runtime_and_protocol_never_import_persistence(module):
    source=(AGENT_DIR/module).read_text(encoding='utf-8')
    imports=re.findall(r'^\s*(?:from|import)\s+.*$',source,re.M)
    assert not [line for line in imports if re.search(r'\bevents\b|\bdb\b|AgentEvent|AgentSession',line)],module


def test_16_concurrent_appends_never_duplicate_a_sequence():
    session=events.open_session()
    def work(worker):
        for index in range(5):events.append(session['id'],event('request_started',worker=worker,index=index))
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(work,range(8)))
    sequences=[row['sequence'] for row in events.read(session['id'],limit=1000)]
    assert sequences==list(range(1,41)) and len(set(sequences))==40


def test_runtime_wiring_persists_events_and_keeps_the_existing_consumer():
    session=events.open_session(dataset_id='ds-1')
    interruptions=[];failures=[]
    def existing_consumer(registered):                     # stands in for ACP._on_lifecycle
        if registered.get('type')=='reader_stopped':interruptions.append(registered)
    runtime=Runtime(sys.executable,[STUB,'noisy_stderr'],
        on_lifecycle=events.fanout(existing_consumer,
            events.persisting_consumer(session['id'],dataset_id='ds-1',on_error=failures.append)))
    runtime.start()
    try:
        runtime.request('session/new',{'cwd':'.','mcpServers':[]},timeout=10)
    finally:
        runtime.stop();runtime.join_reader(5)
    assert [entry['type'] for entry in interruptions]==['reader_stopped']
    types=[row['event_type'] for row in events.read(session['id'],limit=1000)]
    assert 'process_started' in types and 'request_completed' in types and 'process_stopped' in types
    assert not [name for name in types if 'stderr' in name or name=='reader_stopped']
    assert failures==[]
    assert len(runtime.diagnostics()['stderr'])>=1


def test_fanout_isolates_a_failing_consumer():
    calls=[];errors=[]
    def broken(registered):raise RuntimeError('consumer down')
    def healthy(registered):calls.append(registered['type'])
    dispatch=events.fanout(broken,healthy,on_error=errors.append)
    dispatch(event('process_started'))
    assert calls==['process_started'] and len(errors)==1 and isinstance(errors[0],RuntimeError)


def test_prune_before_refuses_to_break_the_sequence_cursor():
    session=events.open_session()
    for index in range(5):events.append(session['id'],event('request_started',ident=index))
    with pytest.raises(NotImplementedError,match='游标'):
        events.prune_before(session['id'],3)
    assert [row['sequence'] for row in events.read(session['id'])]==[1,2,3,4,5]
    assert [row['sequence'] for row in events.read(session['id'],after_sequence=2)]==[3,4,5]
    assert events.latest_sequence(session['id'])==5
