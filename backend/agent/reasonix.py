"""Reasonix adapter: the agent backend over the CLI's line-delimited JSON-RPC session protocol.

It is built directly on ``agent.runtime`` and ``agent.protocol``, deliberately not on the
workbench's legacy caller, so the agent core keeps exactly one transport and one wire parser.
The adapter owns the handshake sequence, the backend contract and the launch command of the
runtime it speaks to; the binary path, working directory and process environment are injected,
so nothing here depends on where the workbench keeps its files or how it names its concepts.

It implements the whole backend contract: bind, start (a new session or a resumed one), send,
cancel, answer/refuse an inbound interaction, status and stop. Cancelling abandons a turn and
keeps the session; stopping is the only move that discards the runtime.
"""
import os
import threading
import time
import uuid
from dataclasses import dataclass,replace
from typing import Callable, Mapping

from . import protocol
from .backend import CancelledError,RuntimeUnavailable,TurnError
from .runtime import Runtime

PROVIDER='reasonix'
PROTOCOL_VERSION=1
MESSAGE_CHUNK='agent_message_chunk'
TURN_END='end_turn'
TURN_CANCELLED='cancelled'
TOOL_CALL_KINDS=('tool_call','tool_call_update')
TOOL_CALL_KIND='tool_call'
TOOL_PENDING='pending'
TOOL_TERMINAL_STATUSES=('completed','failed')
TOOL_CALL_CAPACITY=50
UNKNOWN_STOP_CAPACITY=16
# One inbound method carries both tool permission and user questions; the two are different
# things to the workbench, so they get different events and different host APIs.
QUESTION_KIND='question'
PERMISSION_KIND='permission'
ANSWER_SELECTED='selected'
ANSWER_CANCELLED='cancelled'
EVENT_QUESTION_REQUESTED='question_requested'
EVENT_QUESTION_SETTLED='question_settled'
EVENT_PERMISSION_REQUESTED='permission_requested'
EVENT_PERMISSION_SETTLED='permission_settled'
SETTLED_EVENT_BY_KIND={QUESTION_KIND:EVENT_QUESTION_SETTLED,PERMISSION_KIND:EVENT_PERMISSION_SETTLED}
INITIALIZE_TIMEOUT_SECONDS=60.0
SESSION_TIMEOUT_SECONDS=90.0
RELOAD_TIMEOUT_SECONDS=90.0
PROMPT_TIMEOUT_SECONDS=1200.0
CLOSE_TIMEOUT_SECONDS=5.0
DETAIL_LIMIT=200
INBOX_CAPACITY=50
LIFECYCLE_CAPACITY=50
VENDOR_META_KEY='reasonix.io'
RELOAD_CAPABILITY_KEY='sessionReloadExtensions'

# --- execution budget -----------------------------------------------------
#
# How long a turn may stay unproductive, how long a tool may run, and how long the turn may last at
# all. The budget is caller policy: a composition injects it, and with none the adapter keeps the
# timing behaviour it had before (one transport timeout, no semantic supervision).
BUDGET_TRANSPORT_GRACE_SECONDS=60.0
INTERRUPT_USER_CANCEL='user_cancel'
INTERRUPT_FIRST_ACTIVITY='turn_first_activity_timeout'
INTERRUPT_IDLE='turn_idle_timeout'
INTERRUPT_TOOL='turn_tool_timeout'
INTERRUPT_WALL_CLOCK='turn_wall_clock_timeout'
TIMEOUT_INTERRUPTS=frozenset({INTERRUPT_FIRST_ACTIVITY,INTERRUPT_IDLE,INTERRUPT_TOOL,INTERRUPT_WALL_CLOCK})


@dataclass(frozen=True)
class ExecutionBudget:
    """Time limits for one turn, in seconds.

    ``turn_wall_clock_timeout`` is absolute: it runs from the moment the prompt is sent and is not
    paused by model activity, a running tool or a question waiting on a person. The other three are
    semantic: ``first_activity_timeout`` bounds a turn that has produced nothing yet,
    ``idle_timeout`` bounds a productive turn that went quiet, and ``tool_wait_timeout`` bounds one
    tool call. Nothing here is read from the environment, persisted or exposed to the peer.
    """
    first_activity_timeout: float
    idle_timeout: float
    tool_wait_timeout: float
    turn_wall_clock_timeout: float

    def __post_init__(self):
        for name in ('first_activity_timeout','idle_timeout','tool_wait_timeout','turn_wall_clock_timeout'):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or value<=0:
                raise ValueError('ExecutionBudget.%s 必须是正数秒'%name)


class _TurnState:
    """Supervision state for one in-flight turn. Created per turn, discarded when it ends.

    Waiting is per identity, never a flag: several questions can be open at once and several tools
    can run at once, and the turn only leaves a wait when the last one settles.
    """

    def __init__(self,budget):
        self.budget=budget
        self.started_at=time.monotonic()
        self.first_activity_at=None
        self.last_activity_at=None
        self.pending_interactions=set()
        self.active_tools={}
        self.interrupt_cause=None
        self.accept_peer_output=True

    def deadline(self,now=None):
        """The nearest deadline that applies right now, or None when nothing is running out."""
        now=time.monotonic() if now is None else now
        if self.budget is None:return None
        deadlines=[self.started_at+self.budget.turn_wall_clock_timeout]
        if self.active_tools:
            deadlines.append(min(self.active_tools.values())+self.budget.tool_wait_timeout)
        elif self.pending_interactions:
            # A person is deciding: no ordinary idle deadline runs while a question is open.
            pass
        elif self.first_activity_at is None:
            deadlines.append(self.started_at+self.budget.first_activity_timeout)
        else:
            deadlines.append((self.last_activity_at or self.started_at)+self.budget.idle_timeout)
        return min(deadlines)

    def expired_cause(self,now=None):
        """Which deadline the clock has passed. Wall clock outranks the rest when several do."""
        now=time.monotonic() if now is None else now
        if self.budget is None:return None
        if now>=self.started_at+self.budget.turn_wall_clock_timeout:return INTERRUPT_WALL_CLOCK
        if self.active_tools and now>=min(self.active_tools.values())+self.budget.tool_wait_timeout:
            return INTERRUPT_TOOL
        if self.pending_interactions:return None
        if self.first_activity_at is None:
            return INTERRUPT_FIRST_ACTIVITY if now>=self.started_at+self.budget.first_activity_timeout else None
        if now>=(self.last_activity_at or self.started_at)+self.budget.idle_timeout:return INTERRUPT_IDLE
        return None


@dataclass(frozen=True)
class ReasonixResumeState:
    """What it takes to ask a fresh runtime for an existing session.

    ``runtime_key`` is an opaque caller-owned string. The adapter only ever compares it; it never
    generates, derives, hashes or interprets it.
    """
    session_id: str
    runtime_key: str


@dataclass(frozen=True)
class ReasonixLaunchConfig:
    """Injected launch facts for one runtime.

    ``model`` is both the model reported through ``status()`` and the model the adapter selects
    on the command line, so there is exactly one source of truth for it.
    """
    executable: str
    model: str
    cwd: str
    env: Mapping[str,str]
    runtime_key: str
    resume: ReasonixResumeState|None=None
    client_name: str='gewu-workbench'
    client_version: str='0'
    budget: ExecutionBudget|None=None


def _detail(error):
    return ('%s: %s' % (type(error).__name__,error))[:DETAIL_LIMIT]


def _text(value):
    return value.strip() if isinstance(value,str) else ''


def _failure_detail(failure):
    failure=failure or {}
    return ('%s: %s' % (failure.get('type'),failure.get('detail')))[:DETAIL_LIMIT]


class ReasonixAdapter:
    """One adapter instance owns one runtime process and one remote session.

    It reports through an injected sink and never reads a stream itself: the runtime hands it
    parsed messages and lifecycle events, and translating those into agent output belongs to a
    later step. Start and stop are the only lifecycle moves implemented here.
    """

    def __init__(self,config,on_diagnostic=None):
        if not isinstance(config,ReasonixLaunchConfig):raise TypeError('需要 ReasonixLaunchConfig')
        if not config.executable:raise ValueError('缺少可执行文件')
        if not config.cwd:raise ValueError('缺少工作目录')
        # session/new and session/resume carry this path to the peer, so it must be a real
        # absolute directory instead of something the peer interprets relative to its own start.
        if not os.path.isabs(config.cwd):raise ValueError('工作目录必须是绝对路径：'+config.cwd)
        if not os.path.isdir(config.cwd):raise ValueError('工作目录不存在：'+config.cwd)
        if not _text(config.runtime_key):raise ValueError('缺少 runtime_key')
        # Whether to abandon an unusable context and open a fresh session is the caller's policy,
        # so a mismatched key stops here: no process, no request and no quiet fallback.
        if config.resume is not None:
            if not isinstance(config.resume,ReasonixResumeState):raise TypeError('需要 ReasonixResumeState')
            if not _text(config.resume.session_id):raise ValueError('缺少会话标识')
            if config.resume.runtime_key!=config.runtime_key:
                raise ValueError('runtime_key 与恢复目标不一致')
        self._config=config
        self._sink=lambda event:None
        self._on_diagnostic=on_diagnostic or (lambda kind,line:None)
        self._runtime=None
        self._remote_session_id=None
        self._handshake=None
        self._reload_method=None
        self._started_as=None
        self._ready=False
        self._in_flight=False
        self._turn_active=False
        self._cancel_requested=False
        self._stopping=False
        self._turn_text=[]
        self._ignored_updates={}
        self._tool_calls={}
        self._emit_failure=None
        self._unknown_stop_reasons={}
        self._pending_requests={}
        self._refused_requests={}
        # One turn's supervision state at a time, guarded with its own condition: the reader thread
        # reports activity, the supervisor waits for the nearest deadline, and the caller may cancel
        # — all three meet here and nowhere else.
        self._turn=None
        self._turn_cond=threading.Condition(threading.Lock())
        self._supervisor=None
        # An interrupted turn leaves a runtime whose stream may still carry that turn, so the next
        # send starts a fresh runtime on the same session instead of reusing this one.
        self._needs_resume=False
        # Guards the pending interaction registry and the settle-before-reply-triggered-terminal
        # ordering; the turn terminal waits on it so a settlement can never be overtaken.
        self._interaction_lock=threading.RLock()
        self._exit=None
        self._inbox=[]
        self._lifecycle=[]
        self._lock=threading.RLock()

    # --- backend contract -------------------------------------------------

    def bind(self,emit:Callable[[dict],None]) -> None:
        if not callable(emit):raise TypeError('事件接收器必须可调用')
        self._sink=emit

    def start(self) -> None:
        with self._lock:
            if self._ready:return
            if self._runtime is not None:raise RuntimeUnavailable('运行时已经启动')
            runtime=Runtime(self._config.executable,self.launch_args(),cwd=self._config.cwd,
                env=dict(self._config.env),on_event=self._on_message,
                on_diagnostic=self._on_diagnostic,on_lifecycle=self._on_lifecycle)
            self._runtime=runtime
            try:
                runtime.start()
            except Exception as error:
                self._runtime=None
                raise RuntimeUnavailable('运行时启动失败',detail=_detail(error)) from error
            try:
                self._handshake=self._initialize(runtime)
                if self._config.resume is None:
                    self._remote_session_id=self._create_session(runtime)
                    self._started_as='new'
                else:
                    self._resume(runtime,self._config.resume)
                    self._started_as='resumed'
            except BaseException:
                self._teardown()
                raise
            self._ready=True

    def send(self,text:str) -> None:
        """Run one turn: stream assistant text, then report completion from the peer's answer.

        Completion is never inferred from the text, from silence or from an idle reader: it is the
        ``session/prompt`` response, which the peer sends after the last update.

        A turn that was interrupted — by the caller or by this adapter's own budget — finishes the
        runtime it ran on: the next send builds a fresh one and resumes the same session first, so no
        late output from the interrupted turn can reach a later one.
        """
        if not isinstance(text,str) or not text.strip():raise ValueError('消息需要非空文本')
        self._recover_if_needed()
        runtime=self._runtime
        budget=self._config.budget
        with self._lock:
            if not self._ready or runtime is None or not runtime.alive:
                raise RuntimeUnavailable('会话当前不可发送')
            if self._in_flight:raise TurnError('同一会话已有回合在执行')
            self._in_flight=True
            self._turn_active=True
            self._cancel_requested=False
            self._turn_text=[]
            self._emit_failure=None
        with self._turn_cond:self._turn=_TurnState(budget)
        if budget is not None:self._start_supervisor()
        # The transport ceiling sits above the semantic one, so a supervisor that works always wins
        # and this only fires when supervision itself failed.
        timeout=PROMPT_TIMEOUT_SECONDS if budget is None else budget.turn_wall_clock_timeout+BUDGET_TRANSPORT_GRACE_SECONDS
        started=time.monotonic()
        try:
            try:
                result=runtime.request('session/prompt',
                    {'sessionId':self._remote_session_id,
                     'prompt':[{'type':'text','text':text}]},timeout=timeout)
            except Exception as error:
                raise self._turn_failure(error,started,budget) from error
            # The protocol turn is done. A turn is only delivered when every event it owes was
            # delivered: a consumer failure anywhere, including the turn event itself, fails the
            # turn — and a broken consumer must never be told the turn completed normally.
            if self._emit_failure is not None:
                raise TurnError('回合事件交付失败',detail=_failure_detail(self._emit_failure))
            stop=self._stop_reason(result)
            if not self._turn_settled(stop) or self._emit_failure is not None:
                raise TurnError('回合事件交付失败',detail=_failure_detail(self._emit_failure))
            # Delivery has precedence over the protocol outcome: the caller only hears success or
            # cancellation once the turn's whole outward contract reached the log.
            if stop==TURN_END:return
            # A turn that was interrupted ends as its winner says, whatever words the peer used: the
            # deadline that gave up does not become a caller cancellation just because the peer
            # phrases the abandoned turn as "cancelled".
            cause=self._turn_cause()
            if cause==INTERRUPT_USER_CANCEL:raise CancelledError('本轮已被取消，会话仍可继续')
            if cause in TIMEOUT_INTERRUPTS:
                raise TurnError('回合超过预算已放弃',detail='stop_reason=%s' % (stop or '<missing>'),code=cause)
            # A turn we asked to abandon ends here even when the peer phrases it as an error: the
            # caller asked for it, so a cancellation is the honest answer, not a failure.
            if self._cancel_requested:raise CancelledError('本轮已被取消，会话仍可继续')
            if stop==TURN_CANCELLED:raise CancelledError('本轮已被取消，会话仍可继续')
            self._note_unknown_stop(stop)
            raise TurnError('回合以未识别的终止原因结束',detail='stop_reason=%s' % (stop or '<missing>'))
        finally:
            with self._lock:
                self._in_flight=False
                self._turn_active=False
            self._finish_turn()

    def _turn_failure(self,error,started,budget):
        """Map a failed prompt onto the contract.

        The winner of the interrupt race owns the outcome: a cancel that arrives after a deadline
        already expired cannot rewrite what the deadline reported, and neither can the reverse.
        """
        with self._turn_cond:
            turn=self._turn
            cause=turn.interrupt_cause if turn is not None else None
        if cause==INTERRUPT_USER_CANCEL:return CancelledError('本轮已被本地取消或停止')
        if cause in TIMEOUT_INTERRUPTS:return TurnError('回合超过预算已放弃',detail=_detail(error),code=cause)
        # No interruption was claimed: this is the transport itself failing.
        if self._stopping or self._cancel_requested:return CancelledError('本轮已被本地取消或停止')
        # A live process is not a live session. With the reader gone nothing can ever be answered
        # again, so this runtime is finished for every future request and not only for this turn —
        # which is why it is decided before any budget is read.
        runtime=self._runtime
        if runtime is None or not runtime.alive or not runtime.reader_alive:
            with self._lock:self._ready=False
            return RuntimeUnavailable('回合执行期间运行时失去响应通道',detail=_detail(error))
        reference=PROMPT_TIMEOUT_SECONDS if budget is None else budget.turn_wall_clock_timeout
        if time.monotonic()-started>=reference:
            # The budget ran out without the supervisor claiming it: abandon the turn the same way.
            if self._try_interrupt(INTERRUPT_WALL_CLOCK):self._notify_cancel()
            if runtime.alive:return TurnError('回合超过预算已放弃',detail=_detail(error))
        if runtime.alive:return TurnError('回合执行失败',detail=_detail(error))
        with self._lock:self._ready=False
        return RuntimeUnavailable('回合执行期间运行时已退出',detail=_detail(error))

    # --- turn supervision -------------------------------------------------
    #
    # The budget is enforced here and nowhere else: the caller blocks on the protocol request, the
    # reader reports what arrived, and this only compares clocks. Nothing below reads a frame's
    # meaning, and the transport layer is not told that assistant text, tools or questions exist.

    def _start_supervisor(self):
        thread=threading.Thread(target=self._supervise,name='reasonix-budget',daemon=True)
        with self._turn_cond:self._supervisor=thread
        thread.start()

    def _supervise(self):
        """Wait for the nearest deadline that applies, then claim the turn's interruption."""
        while True:
            with self._turn_cond:
                turn=self._turn
                if turn is None or turn.budget is None or turn.interrupt_cause is not None:return
                deadline=turn.deadline()
                if deadline is None:return
                remaining=deadline-time.monotonic()
                if remaining>0:
                    self._turn_cond.wait(remaining);continue
                cause=turn.expired_cause()
            if cause is None:continue
            if self._try_interrupt(cause):self._notify_cancel()
            return

    def _try_interrupt(self,cause):
        """Claim the turn's one interruption. First writer wins, so a cancel racing a deadline
        produces exactly one cause, one notification and one outcome — never two."""
        with self._turn_cond:
            turn=self._turn
            if turn is None or turn.interrupt_cause is not None:return False
            turn.interrupt_cause=cause
            # From this moment the runtime's further output belongs to a turn nobody is listening to.
            turn.accept_peer_output=False
            self._turn_cond.notify_all()
            return True

    def _notify_cancel(self):
        """Ask the peer to abandon the turn in flight. Only the winner of the race calls this."""
        runtime=self._runtime
        if runtime is None or not runtime.alive:return
        try:runtime.notify('session/cancel',{'sessionId':self._remote_session_id})
        except Exception as error:self._on_diagnostic('cancel',_detail(error))

    def _finish_turn(self):
        """End supervision for the turn and, if it was interrupted, finish its runtime too.

        An interrupted turn leaves a wire runtime that may still deliver that turn's output, so it is
        stopped as part of this turn's unwind rather than reused. The session identity is kept and
        the next send resumes it on a fresh runtime.
        """
        with self._turn_cond:
            turn=self._turn
            self._turn=None
            self._turn_cond.notify_all()
        if turn is None or turn.interrupt_cause is None:return
        self._needs_resume=True
        self._close_remote_session()
        self._teardown()

    def _close_remote_session(self):
        """Best-effort close of the peer session, bounded, before the runtime is torn down."""
        runtime=self._runtime
        session_id=self._remote_session_id
        if runtime is None or not session_id or not runtime.alive:return
        try:runtime.request('session/close',{'sessionId':session_id},timeout=CLOSE_TIMEOUT_SECONDS)
        except Exception as error:self._on_diagnostic('close',_detail(error))

    def _recover_if_needed(self):
        """Resume the same session on a fresh runtime before any prompt is sent on a tainted one."""
        if not self._needs_resume:return
        self._config=replace(self._config,resume=ReasonixResumeState(session_id=self._remote_session_id,
                                                                    runtime_key=self._config.runtime_key))
        try:
            self.start()
        except Exception:
            # No session/new fallback: silently opening a different session would drop the context
            # the caller is still talking to. The adapter stays unusable until it is rebuilt.
            self._needs_resume=True
            raise
        self._needs_resume=False

    def _turn_cause(self):
        with self._turn_cond:
            turn=self._turn
            return turn.interrupt_cause if turn is not None else None

    def _accepts_peer_output(self):
        with self._turn_cond:
            turn=self._turn
            return turn is None or turn.accept_peer_output

    def _note_activity(self,event):
        """Update the turn's supervision state from what one event means.

        Noise never reaches this method: diagnostics, raw frames and lifecycle events are not emitted
        as outward events, so they cannot refresh a deadline by accident.
        """
        kind=event.get('type')
        now=time.monotonic()
        with self._turn_cond:
            turn=self._turn
            if turn is None:return
            if kind=='assistant_text_delta':
                if turn.first_activity_at is None:turn.first_activity_at=now
                turn.last_activity_at=now
            elif kind=='tool_call_started':
                if turn.first_activity_at is None:turn.first_activity_at=now
                turn.last_activity_at=now
                call_id=event.get('tool_call_id')
                if call_id:turn.active_tools[call_id]=now
            elif kind=='tool_call_settled':
                turn.last_activity_at=now
                call_id=event.get('tool_call_id')
                if call_id:turn.active_tools.pop(call_id,None)
            elif kind in ('question_requested','permission_requested'):
                if turn.first_activity_at is None:turn.first_activity_at=now
                interaction_id=event.get('interaction_id')
                if interaction_id:turn.pending_interactions.add(interaction_id)
            elif kind in ('question_settled','permission_settled'):
                interaction_id=event.get('interaction_id')
                if interaction_id:turn.pending_interactions.discard(interaction_id)
                turn.last_activity_at=now
            else:return
            self._turn_cond.notify_all()

    def _stop_reason(self,result):
        """The peer's terminal reason. A missing, null or non-string value is simply absent."""
        raw=result.get('stopReason') if isinstance(result,dict) else None
        return _text(raw) if isinstance(raw,str) else ''

    def _note_unknown_stop(self,stop):
        """Count outcomes we do not recognise instead of guessing what they mean."""
        key=stop or 'missing'
        self._unknown_stop_reasons[key]=self._unknown_stop_reasons.get(key,0)+1
        while len(self._unknown_stop_reasons)>UNKNOWN_STOP_CAPACITY:
            self._unknown_stop_reasons.pop(next(iter(self._unknown_stop_reasons)))

    def _emit(self,event,from_peer=True):
        """Hand one event to the consumer without letting the consumer break the transport.

        An interrupted turn closes the gate on peer output: the runtime may still deliver that turn's
        frames, and none of them may reach a later turn's consumer. Events this adapter raises about
        its own cleanup (an interaction it settled) are not peer output and always go out, so the
        interactions already exposed can still be closed exactly once.

        Only ordinary exceptions are absorbed: a broken sink must not kill the runtime reader,
        because the peer's connection is still perfectly usable. The first failure is recorded
        for the turn and further outward emits are suppressed, so one bad callback cannot turn
        into an exception storm for the rest of the turn.
        """
        self._note_activity(event)
        if from_peer and not self._accepts_peer_output():return True
        if self._emit_failure is not None:return False
        try:
            self._sink(event)
        except Exception as error:
            self._emit_failure={'type':type(error).__name__,'detail':str(error)[:DETAIL_LIMIT]}
            return False
        return True

    def _turn_settled(self,stop):
        """Report the turn's terminal outcome, read straight off the peer's response.

        Taken under the interaction lock so a settlement the peer's answer already triggered is
        always recorded before the terminal it caused.
        """
        event={'type':'turn_settled'}
        if stop:event['stop_reason']=stop
        with self._interaction_lock:
            # The turn's own terminal is this adapter's report about the turn it supervised, not more
            # business output from the peer, so it is delivered even after an interruption: a caller
            # must always hear how the turn it started ended.
            return self._emit(event,from_peer=False)

    def cancel(self) -> bool:
        """Abandon the turn in flight, keeping the peer session.

        This is a request, not a guarantee: the peer stops waiting, but work it already dispatched
        (an MCP tool, for instance) may keep running. Nothing here is a teardown — but a turn this
        actually interrupted leaves its runtime finished, and the next send resumes the session on a
        fresh one. Claiming the interruption is what makes a cancel and a deadline mutually
        exclusive: the second one to arrive does not notify again and does not change the outcome.
        """
        with self._lock:
            runtime=self._runtime
            # Readiness is deliberately not consulted: stop() clears it before asking for the
            # in-flight turn to be abandoned, and that abandon is exactly what must still happen.
            if runtime is None or not self._turn_active:return False
            if self._cancel_requested:return True          # idempotent: one notification per turn
            self._cancel_requested=True
        # A waiting interaction cannot be answered any more. Refusing it now keeps the peer from
        # waiting, and settles it before the terminal this cancellation is about to cause.
        if self._try_interrupt(INTERRUPT_USER_CANCEL):
            self._invalidate_interactions('cancelled')
            self._notify_cancel()
        return True

    def stop(self) -> None:
        """Release everything: abandon the turn, close the peer session, tear the runtime down.

        A running tool may outlive this, so nothing here waits for it; the wait that does exist is
        bounded by the close timeout.
        """
        with self._lock:
            runtime=self._runtime
            if runtime is None:return
            self._stopping=True
            self._ready=False
            in_flight=self._turn_active
        if in_flight:self.cancel()
        self._invalidate_interactions('stopped')
        self._close_remote_session()
        self._teardown()

    def _invalidate_interactions(self,outcome):
        """Nothing can answer these any more, so refuse them instead of leaving the peer waiting.

        The whole refuse-and-settle stays inside the interaction lock for the same reason an answer
        does: the reply wakes the peer, whose response can reach the turn terminal thread before
        this loop finishes. Holding the lock means the settlement always precedes the terminal it
        is about to cause, in the cancel path exactly as in the answer path.
        """
        with self._interaction_lock:
            pending=list(self._pending_requests.items())
            self._pending_requests.clear()
            for interaction_id,request in pending:
                self._reply(request['wire_request_id'],{'outcome':{'outcome':ANSWER_CANCELLED}})
                self._settled_event(SETTLED_EVENT_BY_KIND.get(request['kind'],EVENT_QUESTION_SETTLED),
                                    interaction_id,outcome=outcome)

    def status(self) -> dict:
        runtime=self._runtime
        # A stopped process keeps its last pid inside the process object; status reports only
        # the current runtime, and historical facts stay in diagnostics().
        live=bool(runtime is not None and runtime.alive)
        return {'provider':PROVIDER,'model':self._config.model,
                'remote_session_id':self._remote_session_id,
                'runtime_key':self._config.runtime_key,
                'pid':runtime.pid if live else None,
                'alive':live,
                'ready':self._ready}

    def launch_args(self) -> list:
        """The full command line for this runtime, derived from the injected configuration."""
        return ['acp','--model',self._config.model]

    # --- read-only observability -----------------------------------------

    def diagnostics(self) -> dict:
        """Read-only transport facts, including what a torn-down runtime left behind."""
        runtime=self._runtime
        return {'provider':PROVIDER,'remote_session_id':self._remote_session_id,'ready':self._ready,
                'started_as':self._started_as,'reload_method':self._reload_method,
                'in_flight':self._in_flight,'turn_chars':sum(len(part) for part in self._turn_text),
                'emit_failure':self._emit_failure,
                'unknown_stop_reasons':dict(self._unknown_stop_reasons),
                'pending_requests':sorted(self._pending_requests),
                'refused_requests':dict(self._refused_requests),
                'ignored_updates':dict(self._ignored_updates),
                'tool_classes':self._tool_class_counts(),
                'tool_frames_seen':len(self._tool_calls),
                'reader_alive':bool(runtime is not None and runtime.reader_alive),
                'inbox':list(self._inbox),'lifecycle':list(self._lifecycle),'exit':self._exit,
                'runtime':runtime.diagnostics() if runtime is not None
                else {'stderr':[],'protocol':[],'lifecycle':[]}}

    def request_methods(self) -> list:
        """Methods sent to the peer, in order. Derived from the runtime's own lifecycle ring."""
        runtime=self._runtime
        source=runtime.lifecycle_tail() if runtime is not None else (self._exit or {}).get('lifecycle') or []
        return [event.get('method') for event in source if event.get('type')=='request_started']

    # --- handshake --------------------------------------------------------

    def _initialize(self,runtime):
        params={'protocolVersion':PROTOCOL_VERSION,'clientCapabilities':{},
                'clientInfo':{'name':self._config.client_name,'version':self._config.client_version}}
        try:result=runtime.request('initialize',params,timeout=INITIALIZE_TIMEOUT_SECONDS)
        except Exception as error:
            raise RuntimeUnavailable('initialize 失败',detail=_detail(error)) from error
        if not isinstance(result,dict):
            raise RuntimeUnavailable('initialize 未返回协商结果')
        negotiated=result.get('protocolVersion')
        if negotiated!=PROTOCOL_VERSION:
            raise RuntimeUnavailable('initialize 协商的协议版本不受支持',
                detail='expected=%s negotiated=%s' % (PROTOCOL_VERSION,negotiated))
        return result

    def _create_session(self,runtime):
        try:result=runtime.request('session/new',{'cwd':self._config.cwd,'mcpServers':[]},
                                   timeout=SESSION_TIMEOUT_SECONDS)
        except Exception as error:
            raise RuntimeUnavailable('创建会话失败',detail=_detail(error)) from error
        session_id=result.get('sessionId') if isinstance(result,dict) else None
        if not _text(str(session_id)):raise RuntimeUnavailable('创建会话未返回会话标识')
        return str(session_id)

    def _resume(self,runtime,resume):
        """Resume, then re-establish the extension surface before the session counts as ready."""
        self._require_capability('resume','该运行时未声明 session/resume 能力')
        method=self._read_reload_method()
        try:
            result=runtime.request('session/resume',
                {'sessionId':resume.session_id,'cwd':self._config.cwd,'mcpServers':[]},
                timeout=SESSION_TIMEOUT_SECONDS)
        except Exception as error:
            raise RuntimeUnavailable('恢复会话失败',detail=_detail(error)) from error
        if not isinstance(result,dict):
            raise RuntimeUnavailable('恢复会话未返回会话状态')
        try:
            reloaded=runtime.request(method,{'sessionId':resume.session_id},timeout=RELOAD_TIMEOUT_SECONDS)
        except Exception as error:
            raise RuntimeUnavailable('恢复会话后重载扩展失败',detail=_detail(error)) from error
        if not isinstance(reloaded,dict):
            raise RuntimeUnavailable('重载扩展未返回结果对象')
        self._reload_method=method
        # The peer does not echo the session id, so the resumed identity is the requested one.
        self._remote_session_id=resume.session_id

    # --- capabilities -----------------------------------------------------

    def _capabilities(self):
        value=(self._handshake or {}).get('agentCapabilities')
        return value if isinstance(value,dict) else {}

    def _require_capability(self,key,message):
        if key not in (self._capabilities().get('sessionCapabilities') or {}):
            raise RuntimeUnavailable(message,detail='sessionCapabilities.%s 未广告' % key)

    def _read_reload_method(self):
        """The vendor method name comes from the handshake, never from a guess in this file."""
        vendor=(self._capabilities().get('_meta') or {}).get(VENDOR_META_KEY)
        entry=(vendor or {}).get(RELOAD_CAPABILITY_KEY) if isinstance(vendor,dict) else None
        method=_text((entry or {}).get('method')) if isinstance(entry,dict) else ''
        if not method:
            raise RuntimeUnavailable('该运行时未声明扩展重载方法',
                detail='%s.%s.method 未广告' % (VENDOR_META_KEY,RELOAD_CAPABILITY_KEY))
        return method

    # --- internals --------------------------------------------------------

    def _teardown(self):
        runtime=self._runtime
        self._runtime=None
        self._ready=False
        # A request id belongs to one process: nothing can answer it once the runtime is gone.
        with self._interaction_lock:
            self._pending_requests.clear()
        if runtime is None:return
        try:runtime.stop()
        except Exception:pass
        try:runtime.join_reader()
        except Exception:pass
        self._exit={'alive':runtime.alive,'returncode':runtime.returncode,'pid':runtime.pid,
                    'lifecycle':runtime.lifecycle_tail()}

    def _on_message(self,message):
        """Normalize what this step supports: assistant text, one delta at a time.

        Nothing is coalesced here — the bridge owns that — and nothing unmapped is allowed to look
        like assistant output. Unknown kinds are counted so they stay visible without guessing.
        """
        method=protocol.method_of(message)
        self._inbox.append(method or 'unparsed')
        del self._inbox[:-INBOX_CAPACITY]
        if method==protocol.PERMISSION and 'id' in message:
            self._handle_interaction(message)
            return
        if method!=protocol.SESSION_UPDATE:return
        update=protocol.update_of(message)
        kind=protocol.update_kind(update)
        if kind in TOOL_CALL_KINDS:
            self._note_tool_frame(update)
            return
        if kind!=MESSAGE_CHUNK:
            key=kind or 'unknown'
            self._ignored_updates[key]=self._ignored_updates.get(key,0)+1
            return
        text=protocol.chunk_text(update)
        if not text:return
        self._turn_text.append(text)
        self._emit({'type':'assistant_text_delta','text':text})

    def _note_tool_frame(self,update):
        """Track one tool frame and, when it qualifies, report its host lifecycle.

        Only a real MCP invocation has a workbench lifecycle, and only a start frame can establish
        that: an update carries no rawInput, so it can never identify itself and must never be used
        to promote a call. Each call reports at most one start and one settle; a repeat or a later
        contradicting terminal stays a diagnostic, because the wire has only been verified to go
        pending -> completed|failed.
        """
        call_id=protocol.tool_call_id(update)
        if not call_id:return None
        record=self._tool_calls.get(call_id)
        if record is None:
            record={'state':{},'started':False,'settled':False}
            self._tool_calls[call_id]=record
            while len(self._tool_calls)>TOOL_CALL_CAPACITY:
                self._tool_calls.pop(next(iter(self._tool_calls)))
        record['state']=protocol.merge_tool_state(record['state'],update)
        classification=protocol.classify_tool_call(record['state'])
        if classification!=protocol.CLASS_MCP_TOOL_CALL:return classification
        if protocol.update_kind(update)==TOOL_CALL_KIND:self._start_tool_call(call_id,record,update)
        else:self._settle_tool_call(call_id,record,update)
        return classification

    def _start_tool_call(self,call_id,record,update):
        """Open the outward lifecycle, but only once and only if the start was really delivered."""
        if record['started']:return False
        status=protocol.tool_status(update)
        if status!=TOOL_PENDING:return False
        record['started']=self._emit(self._tool_event('tool_call_started',call_id,record['state'],status))
        return record['started']

    def _settle_tool_call(self,call_id,record,update):
        """Close a lifecycle that was actually opened outward: a classified call is not the same
        fact as a started one, so an unreported start can never be followed by a reported end."""
        if record['settled'] or not record['started']:return False
        status=protocol.tool_status(update)
        if status not in TOOL_TERMINAL_STATUSES:return False
        record['settled']=self._emit(self._tool_event('tool_call_settled',call_id,record['state'],status))
        return record['settled']

    def _tool_event(self,event_type,call_id,state,status):
        """The first-version lifecycle payload: identity and the host status, nothing else.

        Tool inputs and outputs are deliberately absent — everything written here is persisted, and
        no retention, size or redaction policy has been decided for business data yet. Fields can
        be added later without changing an event name.
        """
        raw_input=state.get('rawInput') if isinstance(state.get('rawInput'),dict) else {}
        target=protocol.mcp_tool_target(raw_input)
        event={'type':event_type,'tool_call_id':call_id,'host_status':status}
        if target is not None:event['server'],event['tool']=target
        capability_id=raw_input.get('capability_id')
        if isinstance(capability_id,str) and capability_id:event['capability_id']=capability_id
        return event

    def _tool_class_counts(self):
        """Distinct calls seen, grouped by classification. Nothing here claims an outcome."""
        counts={}
        for record in self._tool_calls.values():
            key=protocol.classify_tool_call(record['state'])
            counts[key]=counts.get(key,0)+1
        return counts

    # --- inbound interaction requests -------------------------------------

    def _handle_interaction(self,message):
        """Answer one inbound permission/question request, or refuse it.

        The peer sends a JSON-RPC request with an id, so ignoring it would leave the turn blocked
        until the peer's own timeout. Everything answerable is announced under a backend-neutral
        interaction id; everything that is not is refused explicitly instead of being dropped.
        """
        wire_request_id=message['id']
        params=protocol.params_of(message)
        if protocol.permission_is_question(params):
            if protocol.permission_is_multi(params):
                return self._refuse_request(wire_request_id,'multi_question')
            builder,kind=(self._question_event,QUESTION_KIND)
        else:
            builder,kind=(self._permission_event,PERMISSION_KIND)
        interaction_id=uuid.uuid4().hex
        payload=builder(interaction_id,params)
        if payload is None:return self._refuse_request(wire_request_id,'malformed_'+kind)
        return self._announce(interaction_id,wire_request_id,kind,payload)

    def _question_event(self,interaction_id,params):
        """The question as the workbench should see it: readable text plus answerable option ids."""
        options=protocol.question_options(params)
        question=protocol.permission_question_text(params)
        if not options or not question:return None
        payload={'type':EVENT_QUESTION_REQUESTED,'interaction_id':interaction_id,
                 'question':question,'multi':False,'options':options}
        question_id=protocol.permission_question_id(params)
        if question_id:payload['question_id']=question_id
        return payload

    def _permission_event(self,interaction_id,params):
        """A permission request. Only fields the wire actually carries, never an inferred target:
        the real tool-level shape has not been verified, so nothing is claimed about the tool."""
        options=protocol.permission_wire_options(params)
        if not options:return None
        payload={'type':EVENT_PERMISSION_REQUESTED,'interaction_id':interaction_id,'options':options}
        tool=protocol.permission_tool(params)
        tool_call_id=tool.get('toolCallId')
        if isinstance(tool_call_id,str) and tool_call_id:payload['tool_call_id']=tool_call_id
        title=tool.get('title')
        if isinstance(title,str) and title:payload['title']=title
        return payload

    def _announce(self,interaction_id,wire_request_id,kind,payload):
        """Publish a request and remember it, or refuse it when nobody could have seen it."""
        option_ids=[option['option_id'] for option in payload['options']]
        with self._interaction_lock:
            self._pending_requests[interaction_id]={'wire_request_id':wire_request_id,
                                                    'kind':kind,'option_ids':option_ids}
            delivered=self._emit(payload)
            if not delivered:self._pending_requests.pop(interaction_id,None)
        if delivered:return payload
        # The consumer never received the request, so no answer can ever arrive: refuse it now
        # rather than leave the peer waiting on a decision that cannot come.
        self._reply(wire_request_id,{'outcome':{'outcome':ANSWER_CANCELLED}})
        self._note_refusal('undelivered_'+kind)
        return None

    def _refuse_request(self,wire_request_id,reason):
        """Refuse something we cannot represent, so the peer is never left waiting."""
        self._note_refusal(reason)
        self._reply(wire_request_id,{'outcome':{'outcome':ANSWER_CANCELLED}})
        return None

    def _note_refusal(self,reason):
        self._refused_requests[reason]=self._refused_requests.get(reason,0)+1
        self._on_diagnostic('interaction','refused reason=%s' % reason)

    def _reply(self,wire_request_id,result):
        """Answer the peer's own request. The wire id never leaves this adapter."""
        runtime=self._runtime
        if runtime is None or not runtime.alive:return False
        try:
            runtime.respond(wire_request_id,result)
            return True
        except Exception as error:
            self._on_diagnostic('interaction',_detail(error))
            return False

    # --- host answers ------------------------------------------------------
    #
    # The caller addresses an interaction by its backend-neutral id. The peer's request id stays
    # private to this adapter, so nothing outside has to know how this backend addresses things.

    def answer_question(self,interaction_id,option_id):
        return self._answer(interaction_id,option_id,QUESTION_KIND,EVENT_QUESTION_SETTLED)

    def cancel_question(self,interaction_id):
        return self._cancel(interaction_id,QUESTION_KIND,EVENT_QUESTION_SETTLED)

    def answer_permission(self,interaction_id,option_id):
        return self._answer(interaction_id,option_id,PERMISSION_KIND,EVENT_PERMISSION_SETTLED)

    def cancel_permission(self,interaction_id):
        return self._cancel(interaction_id,PERMISSION_KIND,EVENT_PERMISSION_SETTLED)

    def _answer(self,interaction_id,option_id,kind,event_type):
        """Claim the interaction, answer the peer, and settle it — all before any terminal it
        triggers can be delivered, so the settlement can never be overtaken."""
        with self._interaction_lock:
            request=self._require_pending(interaction_id,kind,option_id)
            if not self._reply(request['wire_request_id'],
                               {'outcome':{'outcome':ANSWER_SELECTED,'optionId':option_id}}):
                raise RuntimeUnavailable('交互应答发送失败')
            self._pending_requests.pop(interaction_id,None)
            self._settled_event(event_type,interaction_id,outcome=ANSWER_SELECTED,option_id=option_id)
        return True

    def _cancel(self,interaction_id,kind,event_type):
        with self._interaction_lock:
            request=self._require_pending(interaction_id,kind)
            if not self._reply(request['wire_request_id'],{'outcome':{'outcome':ANSWER_CANCELLED}}):
                raise RuntimeUnavailable('交互应答发送失败')
            self._pending_requests.pop(interaction_id,None)
            self._settled_event(event_type,interaction_id,outcome=ANSWER_CANCELLED)
        return True

    def _require_pending(self,interaction_id,kind,option_id=None):
        """Every invalid answer is a caller error: unknown, wrong kind, answered twice, bad option."""
        request=self._pending_requests.get(interaction_id)
        if request is None:raise ValueError('该交互请求不存在或已结束：'+str(interaction_id))
        if request['kind']!=kind:raise ValueError('该交互请求的类型不匹配：'+str(interaction_id))
        if option_id is not None and option_id not in request['option_ids']:
            raise ValueError('所选答案不属于该交互请求：'+str(option_id))
        return request

    def _settled_event(self,event_type,interaction_id,**fields):
        """The host sent its response. This says nothing about the turn or the tool."""
        return self._emit({'type':event_type,'interaction_id':interaction_id,**fields},from_peer=False)

    def _on_lifecycle(self,event):
        self._lifecycle.append(event.get('type'))
        del self._lifecycle[:-LIFECYCLE_CAPACITY]
