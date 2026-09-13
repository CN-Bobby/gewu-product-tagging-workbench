"""Agent bridge: owns agent sessions and turns backend output into persisted events.

The bridge knows two things only: the backend contract and the event store. It does not know
which agent runtime sits underneath, does not read a process stream, does not parse a wire
format, and does not serve HTTP. Transport stays outside so the same bridge can be driven by
SSE, a CLI or a test.
"""
import threading
import time
import uuid
from datetime import datetime,timezone
from . import events as store
from .backend import AgentBackend

BRIDGE_SOURCE='bridge'
ASSISTANT_SOURCE='assistant'
TOOL_SOURCE='tool'
SOURCE_BY_PREFIX=(('assistant_',ASSISTANT_SOURCE),('tool_',TOOL_SOURCE))
OUTPUT_FIELDS_EXCLUDED=('type','session_id','timestamp','source')

STATUS_CREATED='created'
STATUS_STARTING='starting'
STATUS_ACTIVE='active'
STATUS_STOPPING='stopping'
STATUS_STOPPED='stopped'
STATUS_FAILED='failed'

# Explicit state machine. A move that is not listed is refused instead of quietly corrected;
# created -> stopping exists so a session that was never started can still be closed.
ALLOWED_TRANSITIONS={
    STATUS_CREATED:{STATUS_STARTING,STATUS_STOPPING,STATUS_FAILED},
    STATUS_STARTING:{STATUS_ACTIVE,STATUS_FAILED},
    STATUS_ACTIVE:{STATUS_STOPPING,STATUS_FAILED},
    STATUS_STOPPING:{STATUS_STOPPED,STATUS_FAILED},
    STATUS_STOPPED:set(),
    STATUS_FAILED:set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class AgentBridgeError(Exception):
    """Base for every failure the bridge reports to its caller."""


class SessionNotFound(AgentBridgeError):pass
class SessionBusy(AgentBridgeError):pass
class InvalidSessionState(AgentBridgeError):pass
class BackendStartError(AgentBridgeError):pass
class BackendSendError(AgentBridgeError):pass
class BackendStopError(AgentBridgeError):pass


class AgentBridge:
    """Create and drive agent sessions; report everything as durable events.

    Sessions are owned here: the bridge creates the record, advances its status, drives the
    backend, and writes both its own events and the backend's output into one event log. One
    session runs one turn at a time.
    """

    def __init__(self,backend_factory,dataset_id=None):
        if not callable(backend_factory):raise ValueError('需要提供 backend 工厂')
        self._factory=backend_factory
        self._dataset_id=dataset_id
        self._backends={}
        self._datasets={}
        self._busy=set()
        self._lock=threading.Lock()

    # --- sessions ---------------------------------------------------------

    def create_session(self,provider=None,model=None,dataset_id=None):
        dataset_id=dataset_id or self._dataset_id
        session=store.open_session(dataset_id=dataset_id,provider=provider,model=model,status=STATUS_CREATED)
        self._datasets[session['id']]=dataset_id
        self._record(session['id'],'session_created',provider=provider,model=model)
        return store.get_session(session['id'])

    def get_session(self,session_id):
        return store.get_session(session_id)

    def status(self,session_id):
        session=self._require(session_id)
        backend=self._backends.get(session_id)
        try:backend_status=backend.status() if backend else None
        except Exception as error:backend_status={'error':type(error).__name__}
        return {'session_id':session_id,'status':session['status'],
                'remote_session_id':session.get('remote_session_id'),'backend':backend_status}

    def start(self,session_id):
        self._require(session_id)
        self._transition(session_id,STATUS_STARTING)
        self._record(session_id,'session_starting')
        backend=self._factory()
        if not isinstance(backend,AgentBackend):
            error=BackendStartError('后端不符合 AgentBackend 契约')
            self._fail(session_id,'session_failed',error)
            raise error
        backend.bind(lambda event:self._on_backend_event(session_id,event))
        self._backends[session_id]=backend
        try:backend.start()
        except Exception as error:
            self._fail(session_id,'session_failed',error)
            raise BackendStartError(str(error)) from error
        remote=None
        try:remote=backend.status().get('remote_session_id')
        except Exception:remote=None
        self._transition(session_id,STATUS_ACTIVE,remote_session_id=remote)
        self._record(session_id,'session_started',remote_session_id=remote)
        return store.get_session(session_id)

    def stop(self,session_id):
        session=self._require(session_id)
        if session['status'] in (STATUS_STOPPED,STATUS_FAILED):return session
        self._transition(session_id,STATUS_STOPPING)
        self._record(session_id,'session_stopping')
        backend=self._backends.get(session_id)
        try:
            if backend is not None:backend.stop()
        except Exception as error:
            self._fail(session_id,'session_failed',error)
            raise BackendStopError(str(error)) from error
        self._transition(session_id,STATUS_STOPPED,stopped_at=_now())
        self._record(session_id,'session_stopped')
        self._backends.pop(session_id,None)
        return store.get_session(session_id)

    # --- turns ------------------------------------------------------------

    def send(self,session_id,text):
        session=self._require(session_id)
        if session['status']!=STATUS_ACTIVE:raise InvalidSessionState('会话不是可发送状态：'+str(session['status']))
        if not isinstance(text,str) or not text.strip():raise ValueError('消息需要非空文本')
        with self._lock:
            if session_id in self._busy:raise SessionBusy('同一会话已有回合在执行')
            self._busy.add(session_id)
        # Only message metadata is recorded: the prompt text itself is not part of an event.
        message_id=uuid.uuid4().hex
        started=time.perf_counter()
        self._record(session_id,'message_started',message_id=message_id,text_chars=len(text))
        try:
            self._backends[session_id].send(text)
            # Record completion before releasing the turn so ordering cannot interleave.
            return self._record(session_id,'message_completed',message_id=message_id,
                seconds=round(time.perf_counter()-started,3))
        except AgentBridgeError:
            raise
        except Exception as error:
            self._record(session_id,'message_failed',message_id=message_id,error=type(error).__name__)
            self._fail(session_id,'session_failed',error)
            raise BackendSendError(str(error)) from error
        finally:
            with self._lock:self._busy.discard(session_id)

    # --- internals --------------------------------------------------------

    def _require(self,session_id):
        session=store.get_session(session_id)
        if session is None:raise SessionNotFound('Agent 会话不存在：'+str(session_id))
        return session

    def _transition(self,session_id,next_status,**fields):
        session=self._require(session_id)
        current=session['status']
        if next_status not in ALLOWED_TRANSITIONS.get(current,set()):
            raise InvalidSessionState('不允许的状态迁移：'+current+' → '+next_status)
        return store.update_session(session_id,status=next_status,**fields)

    def _fail(self,session_id,event,error):
        session=self._require(session_id)
        if STATUS_FAILED in ALLOWED_TRANSITIONS.get(session['status'],set()):
            store.update_session(session_id,status=STATUS_FAILED)
        return self._record(session_id,event,error=type(error).__name__,detail=str(error)[:200])

    def _record(self,session_id,type,source=BRIDGE_SOURCE,**fields):
        event={'type':type,'timestamp':_now(),'session_id':session_id,
               **{key:value for key,value in fields.items() if value is not None}}
        return store.append(session_id,event,source=source,dataset_id=self._datasets.get(session_id))

    def _on_backend_event(self,session_id,event):
        """Route backend output to the log. A typed violation is recorded, never dropped."""
        if not isinstance(event,dict) or not event.get('type'):
            return self._record(session_id,'backend_event_ignored',reason='missing_type')
        type=str(event['type'])
        source=next((name for prefix,name in SOURCE_BY_PREFIX if type.startswith(prefix)),None)
        if source is None:return self._record(session_id,'backend_event_ignored',backend_type=type)
        fields={key:value for key,value in event.items() if key not in OUTPUT_FIELDS_EXCLUDED}
        return self._record(session_id,type,source=source,**fields)
