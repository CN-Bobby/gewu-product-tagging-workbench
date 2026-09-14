"""Append-only agent event log and agent session registry.

This is the only module in backend/agent that may touch persistence: protocol.py and runtime.py
must stay free of the database so the transport can be tested and reused without it. Nothing
here is wired into the workbench yet; M2A only establishes the infrastructure.
"""
import threading
from sqlalchemy import func,select
from ..db import AgentEvent,AgentSession,Session,now,obj,uid

# --- persistence policy ---------------------------------------------------
# Persisted: state transitions and outcomes that explain what happened.
RUNTIME_EVENT_TYPES=frozenset({
    'process_starting','process_started','process_stopping','process_stopped','process_exited',
    'process_killed','request_started','request_completed','request_timeout','request_failed',
    'protocol_error','reader_failed','stderr_failed',
})
# Bridge-owned session and turn events, plus the agent output the UI needs.
BRIDGE_EVENT_TYPES=frozenset({
    'session_created','session_starting','session_started','session_stopping','session_stopped',
    'session_failed','message_started','message_completed','message_failed',
    'message_cancel_requested','message_cancelled','backend_event_ignored',
})
OUTPUT_EVENT_TYPES=frozenset({
    'assistant_text_delta','turn_settled',
    'tool_call_started','tool_call_settled',
    'question_requested','question_settled',
    'permission_requested','permission_settled',
})
PERSISTED_EVENT_TYPES=RUNTIME_EVENT_TYPES|BRIDGE_EVENT_TYPES|OUTPUT_EVENT_TYPES
# Ephemeral: high-frequency bookkeeping and raw stderr stay in the runtime ring buffer.
EPHEMERAL_EVENT_TYPES=frozenset({'reader_closed','stderr_closed','reader_stopped','stderr_line','stdout_line'})
SOURCES=frozenset({'runtime','bridge','tool','system','assistant'})

# Sequence allocation is serialised in-process; the unique index on (session_id, sequence) is
# the durable guard. sqlite cannot allocate max+1 atomically on its own.
_LOCK=threading.Lock()


def should_persist(event_type):
    """Only meaningful events reach the log; ordinary stderr never does."""
    return str(event_type or '') in PERSISTED_EVENT_TYPES


def normalize(event,source='runtime'):
    """Runtime emits {type,timestamp,pid,...}. Keep type explicit and store the rest as payload."""
    if source not in SOURCES:raise ValueError('未知事件来源：'+str(source))
    payload=dict(event or {})
    event_type=str(payload.get('type') or 'unknown')
    payload['type']=event_type
    payload.setdefault('timestamp',now())
    return event_type,payload


def next_sequence(s,session_id):
    current=s.scalar(select(func.max(AgentEvent.sequence)).where(AgentEvent.session_id==session_id))
    return int(current or 0)+1


# --- append / query -------------------------------------------------------

def append(session_id,event,source='runtime',dataset_id=None,persist=None):
    """Append one event and return its stored view, or None when the policy filters it out."""
    if not session_id:raise ValueError('事件需要 session_id')
    event_type,payload=normalize(event,source)
    if persist is None:persist=should_persist(event_type)
    if not persist:return None
    with _LOCK,Session.begin() as s:
        item=AgentEvent(id=uid(),session_id=session_id,sequence=next_sequence(s,session_id),
            event_type=event_type,source=source,payload=payload,dataset_id=dataset_id,created_at=now())
        s.add(item);s.flush()
        return obj(item)


def read(session_id,after_sequence=0,limit=200):
    """Ascending events with sequence > after_sequence, so after_sequence=152 resumes at 153."""
    with Session() as s:
        items=list(s.scalars(select(AgentEvent).where(AgentEvent.session_id==session_id,AgentEvent.sequence>max(0,int(after_sequence))).order_by(AgentEvent.sequence).limit(max(1,min(int(limit),1000)))))
        return [obj(item) for item in items]


def tail(session_id,limit=50):
    with Session() as s:
        items=list(s.scalars(select(AgentEvent).where(AgentEvent.session_id==session_id).order_by(AgentEvent.sequence.desc()).limit(max(1,min(int(limit),1000)))))
        return [obj(item) for item in reversed(items)]


def latest_sequence(session_id):
    with Session() as s:return int(s.scalar(select(func.max(AgentEvent.sequence)).where(AgentEvent.session_id==session_id)) or 0)


def count(session_id):
    with Session() as s:return int(s.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.session_id==session_id)) or 0)


def prune_before(session_id,before_sequence):
    """Reserved cleanup interface, deliberately not implemented: deleting rows would let
    sequence restart at 1 and make an existing after_sequence cursor miss later events. A real
    design needs a high-water mark or a partitioned log first, so this refuses to delete."""
    raise NotImplementedError('清理接口尚未启用：删除会破坏 sequence 游标语义')


# --- session registry -----------------------------------------------------

SESSION_FIELDS=('dataset_id','provider','model','status','remote_session_id','started_at','stopped_at')


def open_session(dataset_id=None,provider=None,model=None,remote_session_id=None,status='active'):
    with Session.begin() as s:
        item=AgentSession(id=uid(),dataset_id=dataset_id,provider=provider,model=model,status=status,
            remote_session_id=remote_session_id,created_at=now(),updated_at=now(),started_at=now())
        s.add(item);s.flush()
        return obj(item)


def update_session(session_id,**fields):
    unknown=[key for key in fields if key not in SESSION_FIELDS]
    if unknown:raise ValueError('不支持修改的字段：'+','.join(sorted(unknown)))
    with Session.begin() as s:
        item=s.get(AgentSession,session_id)
        if item is None:raise ValueError('Agent 会话不存在：'+str(session_id))
        for key,value in fields.items():setattr(item,key,value)
        item.updated_at=now()
        return obj(item)


def close_session(session_id,status='stopped'):
    with Session.begin() as s:
        item=s.get(AgentSession,session_id)
        if item is None:raise ValueError('Agent 会话不存在：'+str(session_id))
        item.status=status;item.stopped_at=now();item.updated_at=item.stopped_at
        return obj(item)


def get_session(session_id):
    with Session() as s:
        item=s.get(AgentSession,session_id)
        return obj(item) if item else None


def list_sessions(dataset_id=None,limit=50):
    with Session() as s:
        statement=select(AgentSession).order_by(AgentSession.updated_at.desc()).limit(max(1,min(int(limit),200)))
        if dataset_id:statement=statement.where(AgentSession.dataset_id==dataset_id)
        return [obj(item) for item in s.scalars(statement)]


# --- runtime wiring -------------------------------------------------------

def persisting_consumer(session_id,dataset_id=None,on_error=None):
    """A lifecycle consumer that stores the events the policy keeps. A storage failure is
    reported to on_error and never allowed to reach the other consumers."""
    def consume(event):
        if not should_persist((event or {}).get('type')):return None
        try:return append(session_id,event,source='runtime',dataset_id=dataset_id)
        except Exception as error:
            if on_error:on_error(error)
            return None
    return consume


def fanout(*consumers,on_error=None):
    """Compose lifecycle consumers so one failing consumer cannot starve the others; this is how
    the existing subscriber (pending-question interruption) keeps working next to the log."""
    def dispatch(event):
        results=[]
        for consumer in consumers:
            if not consumer:continue
            try:results.append(consumer(event))
            except Exception as error:
                results.append(None)
                if on_error:on_error(error)
        return results
    return dispatch
