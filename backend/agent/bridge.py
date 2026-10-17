"""Agent bridge: owns agent sessions and turns backend output into persisted events.

The bridge knows two things only: the backend contract and the event store. It does not know
which agent runtime sits underneath, does not read a process stream, does not parse a wire
format, and does not serve HTTP. Transport stays outside so the same bridge can be driven by
SSE, a CLI or a test.

One session runs one turn at a time. A failed turn keeps the session alive unless the backend
declares the failure fatal, and cancelling a turn is not the same as ending a session.
"""
import threading
import time
import uuid
from datetime import datetime,timezone
from . import events as store
from .backend import AgentBackend,BackendError,CancelledError

BRIDGE_SOURCE='bridge'
ASSISTANT_SOURCE='assistant'
TOOL_SOURCE='tool'
# Interaction requests are neither assistant prose nor tool activity; they reuse the existing
# system category rather than inventing a new source taxonomy for one round.
INTERACTION_SOURCE='system'
# Declared up front so a new bridge event that nobody registers fails loudly instead of
# vanishing: _record checks these sets, and a test asserts they stay inside the store policy.
SESSION_EVENT_TYPES=frozenset({
    'session_created','session_starting','session_started','session_stopping','session_stopped','session_failed',
})
MESSAGE_EVENT_TYPES=frozenset({
    'message_started','message_completed','message_failed','message_cancel_requested','message_cancelled',
})
DIAGNOSTIC_EVENT_TYPES=frozenset({'backend_event_ignored'})
BRIDGE_EMITTED_TYPES=SESSION_EVENT_TYPES|MESSAGE_EVENT_TYPES|DIAGNOSTIC_EVENT_TYPES
OUTPUT_SOURCE_BY_TYPE={
    # Source is the routing and storage category, not a claim about which layer produced it:
    # turn_settled keeps the assistant category it inherited so no new taxonomy is invented here.
    'assistant_text_delta':ASSISTANT_SOURCE,'turn_settled':ASSISTANT_SOURCE,
    'tool_call_started':TOOL_SOURCE,'tool_call_settled':TOOL_SOURCE,
    'question_requested':INTERACTION_SOURCE,'question_settled':INTERACTION_SOURCE,
    'permission_requested':INTERACTION_SOURCE,'permission_settled':INTERACTION_SOURCE,
}
OUTPUT_FIELDS_EXCLUDED=('type','session_id','timestamp','source')

STATUS_CREATED='created'
STATUS_STARTING='starting'
STATUS_ACTIVE='active'
STATUS_STOPPING='stopping'
STATUS_STOPPED='stopped'
STATUS_FAILED='failed'

# Text deltas arrive token by token; buffer them so the log keeps readable chunks instead of one
# row per token. Correctness beats a fixed refresh rate: a short reply is flushed by the events
# that end the turn, never by a background timer.
MAX_DELTA_CHARS=200
MAX_DELTA_INTERVAL_SECONDS=0.5

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
    """Base for every failure the bridge itself reports to its caller."""


class SessionNotFound(AgentBridgeError):pass
class SessionBusy(AgentBridgeError):pass
class InvalidSessionState(AgentBridgeError):pass
class BackendStartError(AgentBridgeError):
    fatal=True
    def __init__(self,message='',*,stage=None,diagnostic=None):
        self.stage=stage
        self.diagnostic=diagnostic
        super().__init__(message or 'Agent 后端启动失败')
class BackendSendError(AgentBridgeError):
    """The backend broke its contract: it raised something that was not a BackendError.
    Treated as a failed turn, not a failed session, so the caller may simply try again."""


class BackendStopError(AgentBridgeError):pass
class AgentEventContractError(AgentBridgeError):
    """The bridge tried to write an event the event store would not keep. A programming error:
    it must surface immediately instead of letting the event disappear silently."""


class AgentBridge:
    """Create and drive agent sessions; report everything as durable events.

    Sessions are owned here: the bridge creates the record, advances its status, drives the
    backend, and writes both its own events and the backend's output into one event log.
    """

    def __init__(self,backend_factory,dataset_id=None):
        if not callable(backend_factory):raise ValueError('需要提供 backend 工厂')
        self._factory=backend_factory
        self._dataset_id=dataset_id
        self._backends={}
        self._datasets={}
        self._running={}
        self._buffers={}
        self._lock=threading.Lock()

    # --- sessions ---------------------------------------------------------

    def create_session(self,provider=None,model=None,dataset_id=None):
        dataset_id=dataset_id or self._dataset_id
        session=store.open_session(dataset_id=dataset_id,provider=provider,model=model,status=STATUS_CREATED)
        # started_at means "the backend became active"; a created session has not started yet.
        session=store.update_session(session['id'],started_at=None)
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
            raise BackendStartError(str(error),stage=getattr(error,'stage',None),
                                    diagnostic=getattr(error,'diagnostic',None)) from error
        remote=None
        try:remote=backend.status().get('remote_session_id')
        except Exception:remote=None
        self._transition(session_id,STATUS_ACTIVE,remote_session_id=remote,started_at=_now())
        self._record(session_id,'session_started',remote_session_id=remote)
        return store.get_session(session_id)

    def stop(self,session_id):
        session=self._require(session_id)
        if session['status'] in (STATUS_STOPPED,STATUS_FAILED):return session
        self._flush_delta(session_id,'stopped')
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
        message_id=uuid.uuid4().hex
        with self._lock:
            if session_id in self._running:raise SessionBusy('同一会话已有回合在执行')
            self._running[session_id]=message_id
        self._buffers.pop(session_id,None)
        # Only message metadata is recorded: the prompt text itself is not part of an event.
        usage_event_id=None
        try:
            from ..usage import start_usage_event
            usage_event_id=start_usage_event(
                kind='agent_turn',module='agent',task_id=session.get('dataset_id'),
                provider=session.get('provider'),model=session.get('model'),
                metadata={'agent_session_id':session_id,'message_id':message_id},
            )
        except Exception:
            pass
        started=time.perf_counter()
        def finish_usage(status):
            try:
                from ..usage import finish_usage_event
                finish_usage_event(usage_event_id,status=status,
                    duration_ms=round((time.perf_counter()-started)*1000),
                    usage_source='agent_provider_usage_unavailable')
            except Exception:
                pass
        try:
            self._record(session_id,'message_started',message_id=message_id,text_chars=len(text))
            self._backends[session_id].send(text)
            self._flush_delta(session_id,'completed')
            result=self._record(session_id,'message_completed',message_id=message_id,seconds=_elapsed(started))
            finish_usage('completed')
            return result
        except CancelledError as error:
            self._flush_delta(session_id,'cancelled')
            self._record(session_id,'message_cancelled',message_id=message_id,seconds=_elapsed(started))
            finish_usage('cancelled')
            raise
        except BackendError as error:
            self._flush_delta(session_id,'failed')
            self._record(session_id,'message_failed',message_id=message_id,error=type(error).__name__,fatal=bool(error.fatal))
            finish_usage('failed')
            if error.fatal:self._fail(session_id,'session_failed',error)
            raise
        except AgentBridgeError:
            finish_usage('failed')
            raise
        except Exception as error:
            self._flush_delta(session_id,'failed')
            self._record(session_id,'message_failed',message_id=message_id,error=type(error).__name__,fatal=False)
            finish_usage('failed')
            raise BackendSendError(str(error)) from error
        finally:
            with self._lock:
                if self._running.get(session_id)==message_id:self._running.pop(session_id,None)

    def cancel(self,session_id):
        """Abort the turn in flight; the session and its runtime stay usable.

        Only the turn is touched, and nothing here blocks on the busy slot, so a cancel can
        arrive from another thread while a send is still running.
        """
        session=self._require(session_id)
        with self._lock:message_id=self._running.get(session_id)
        if message_id is None:return {'session_id':session_id,'cancelled':False,'status':session['status']}
        self._record(session_id,'message_cancel_requested',message_id=message_id)
        backend=self._backends.get(session_id)
        if backend is not None:backend.cancel()
        return {'session_id':session_id,'cancelled':True,'message_id':message_id}

    # --- interactions -----------------------------------------------------
    #
    # Answers travel the same way everything else does: application -> bridge -> backend. The
    # bridge hands over the backend-neutral interaction id and never learns how a backend
    # addresses its peer, so no wire detail reaches a caller.
    #
    # A true return means the interaction was claimed and the peer was told. It says nothing about
    # whether the settlement event reached the log, nor about the turn's outcome: a settlement that
    # was lost surfaces as the turn failing, so this flag never carries two meanings.

    def answer_question(self,session_id,interaction_id,option_id):
        return self._interact(session_id,'answer_question',interaction_id,option_id)

    def cancel_question(self,session_id,interaction_id):
        return self._interact(session_id,'cancel_question',interaction_id)

    def answer_permission(self,session_id,interaction_id,option_id):
        return self._interact(session_id,'answer_permission',interaction_id,option_id)

    def cancel_permission(self,session_id,interaction_id):
        return self._interact(session_id,'cancel_permission',interaction_id)

    def _interact(self,session_id,method,interaction_id,*arguments):
        """Route one answer to the backend that owns this session. The backend validates it, so
        its own error (such as an unknown interaction or an option it never offered) is what the
        caller sees; the bridge adds no second policy of its own."""
        self._require(session_id)
        backend=self._backends.get(session_id)
        if backend is None:raise InvalidSessionState('会话没有可用后端：'+str(session_id))
        return getattr(backend,method)(interaction_id,*arguments)

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
            store.update_session(session_id,status=STATUS_FAILED,stopped_at=_now())
        return self._record(session_id,event,error=type(error).__name__,detail=str(error)[:200])

    def _record(self,session_id,type,source=BRIDGE_SOURCE,**fields):
        """Write one event, or fail loudly. The store may filter unknown types by policy, but the
        bridge must never lose an event it believes it wrote."""
        if type not in BRIDGE_EMITTED_TYPES and type not in OUTPUT_SOURCE_BY_TYPE:
            raise AgentEventContractError('事件类型未登记：'+str(type))
        if not store.should_persist(type):
            raise AgentEventContractError('事件类型不允许持久化：'+str(type))
        event={'type':type,'timestamp':_now(),'session_id':session_id,
               **{key:value for key,value in fields.items() if value is not None}}
        stored=store.append(session_id,event,source=source,dataset_id=self._datasets.get(session_id))
        if stored is None:raise AgentEventContractError('事件未写入 EventStore：'+str(type))
        return stored

    def _buffer_delta(self,session_id,text):
        if not text:return None
        buffer=self._buffers.get(session_id)
        if buffer is None:
            buffer={'text':'','message_id':self._running.get(session_id),'since':time.monotonic(),'merged':0}
            self._buffers[session_id]=buffer
        buffer['text']+=text
        buffer['merged']+=1
        if len(buffer['text'])>=MAX_DELTA_CHARS or time.monotonic()-buffer['since']>=MAX_DELTA_INTERVAL_SECONDS:
            return self._flush_delta(session_id,'threshold')
        return None

    def _flush_delta(self,session_id,reason):
        buffer=self._buffers.pop(session_id,None)
        if not buffer or not buffer['text']:return None
        return self._record(session_id,'assistant_text_delta',source=ASSISTANT_SOURCE,
            text=buffer['text'],merged=buffer['merged'],message_id=buffer['message_id'],flushed_by=reason)

    def _on_backend_event(self,session_id,event):
        """Route backend output to the log. A typed violation is recorded, never dropped."""
        if not isinstance(event,dict) or not event.get('type'):
            self._flush_delta(session_id,'ordered')
            return self._record(session_id,'backend_event_ignored',reason='missing_type')
        type=str(event['type'])
        if type=='assistant_text_delta':return self._buffer_delta(session_id,event.get('text') or '')
        # Any other event closes the text that came before it. Flushing first keeps the stored
        # order equal to the wire's causal order, instead of letting a tool event overtake the
        # short reply that is still sitting in the buffer.
        self._flush_delta(session_id,'completed' if type=='turn_settled' else 'ordered')
        # Only the documented output types carry output; anything else is a contract violation,
        # including a type that merely looks like one.
        source=OUTPUT_SOURCE_BY_TYPE.get(type)
        if source is None:return self._record(session_id,'backend_event_ignored',backend_type=type)
        fields={key:value for key,value in event.items() if key not in OUTPUT_FIELDS_EXCLUDED}
        return self._record(session_id,type,source=source,**fields)


def _elapsed(started):
    return round(time.perf_counter()-started,3)
