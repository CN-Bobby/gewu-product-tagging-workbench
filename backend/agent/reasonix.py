"""Reasonix adapter: the agent backend over the CLI's line-delimited JSON-RPC session protocol.

It is built directly on ``agent.runtime`` and ``agent.protocol``, deliberately not on the
workbench's legacy caller, so the agent core keeps exactly one transport and one wire parser.
The adapter owns the handshake sequence, the backend contract and the launch command of the
runtime it speaks to; the binary path, working directory and process environment are injected,
so nothing here depends on where the workbench keeps its files or how it names its concepts.

Scope of this step: bind, start (a new session or a resumed one), status and stop. Turning text
into a turn (``send``) and aborting one (``cancel``) belong to later steps and raise until then.
"""
import os
import threading
from dataclasses import dataclass
from typing import Callable, Mapping

from . import protocol
from .backend import RuntimeUnavailable,TurnError
from .runtime import Runtime

PROVIDER='reasonix'
PROTOCOL_VERSION=1
MESSAGE_CHUNK='agent_message_chunk'
TOOL_CALL_KINDS=('tool_call','tool_call_update')
TOOL_CALL_CAPACITY=50
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
        self._turn_text=[]
        self._ignored_updates={}
        self._tool_calls={}
        self._emit_failure=None
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
        """
        if not isinstance(text,str) or not text.strip():raise ValueError('消息需要非空文本')
        runtime=self._runtime
        with self._lock:
            if not self._ready or runtime is None or not runtime.alive:
                raise RuntimeUnavailable('会话当前不可发送')
            if self._in_flight:raise TurnError('同一会话已有回合在执行')
            self._in_flight=True
            self._turn_text=[]
            self._emit_failure=None
        try:
            try:
                result=runtime.request('session/prompt',
                    {'sessionId':self._remote_session_id,
                     'prompt':[{'type':'text','text':text}]},timeout=PROMPT_TIMEOUT_SECONDS)
            except Exception as error:
                # A live runtime means only this turn failed; a dead one means the session is gone.
                if runtime.alive:raise TurnError('回合执行失败',detail=_detail(error)) from error
                with self._lock:self._ready=False
                raise RuntimeUnavailable('回合执行期间运行时已退出',detail=_detail(error)) from error
            # The protocol turn is done. A turn is only delivered when every event it owes was
            # delivered: a consumer failure anywhere, including the completion itself, fails the
            # turn — and a broken consumer must never be told the turn completed normally.
            if self._emit_failure is not None:
                raise TurnError('回合事件交付失败',detail=_failure_detail(self._emit_failure))
            if not self._turn_completed(result):
                raise TurnError('回合事件交付失败',detail=_failure_detail(self._emit_failure))
        finally:
            with self._lock:self._in_flight=False

    def _emit(self,event):
        """Hand one event to the consumer without letting the consumer break the transport.

        Only ordinary exceptions are absorbed: a broken sink must not kill the runtime reader,
        because the peer's connection is still perfectly usable. The first failure is recorded
        for the turn and further outward emits are suppressed, so one bad callback cannot turn
        into an exception storm for the rest of the turn.
        """
        if self._emit_failure is not None:return False
        try:
            self._sink(event)
        except Exception as error:
            self._emit_failure={'type':type(error).__name__,'detail':str(error)[:DETAIL_LIMIT]}
            return False
        return True

    def _turn_completed(self,result):
        stop=_text(str((result or {}).get('stopReason'))) if isinstance(result,dict) else ''
        event={'type':'assistant_message_completed'}
        if stop:event['stop_reason']=stop
        return self._emit(event)

    def cancel(self) -> None:
        raise NotImplementedError('cancel 在后续步骤实现')

    def stop(self) -> None:
        with self._lock:
            runtime=self._runtime
            if runtime is None:return
            self._ready=False
            session_id=self._remote_session_id
            if session_id and runtime.alive:
                try:runtime.request('session/close',{'sessionId':session_id},timeout=CLOSE_TIMEOUT_SECONDS)
                except Exception as error:self._on_diagnostic('close',_detail(error))
            self._teardown()

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
        if method!=protocol.SESSION_UPDATE:return
        update=protocol.update_of(message)
        kind=protocol.update_kind(update)
        if kind in TOOL_CALL_KINDS:
            self._classify_tool_frame(update)
            return
        if kind!=MESSAGE_CHUNK:
            key=kind or 'unknown'
            self._ignored_updates[key]=self._ignored_updates.get(key,0)+1
            return
        text=protocol.chunk_text(update)
        if not text:return
        self._turn_text.append(text)
        self._emit({'type':'assistant_text_delta','text':text})

    def _classify_tool_frame(self,update):
        """Classify tool activity for diagnostics only.

        Nothing is emitted: a wire status is the host's disposition for that call, not evidence
        about what the tool itself did, so turning it into a workbench tool outcome would assert
        something this adapter cannot know. The call id is the only correlation key, so a call and
        its updates are merged under it and never under the tool name.
        """
        call_id=protocol.tool_call_id(update)
        if not call_id:return None
        state=protocol.merge_tool_state(self._tool_calls.get(call_id,{}),update)
        self._tool_calls[call_id]=state
        while len(self._tool_calls)>TOOL_CALL_CAPACITY:
            self._tool_calls.pop(next(iter(self._tool_calls)))
        return protocol.classify_tool_call(self._tool_calls[call_id])

    def _tool_class_counts(self):
        """Distinct calls seen, grouped by classification. Nothing here claims an outcome."""
        counts={}
        for state in self._tool_calls.values():
            key=protocol.classify_tool_call(state)
            counts[key]=counts.get(key,0)+1
        return counts

    def _on_lifecycle(self,event):
        self._lifecycle.append(event.get('type'))
        del self._lifecycle[:-LIFECYCLE_CAPACITY]
