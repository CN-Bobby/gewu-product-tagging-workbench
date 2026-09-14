"""Reasonix adapter: the agent backend over the CLI's line-delimited JSON-RPC session protocol.

It is built directly on ``agent.runtime`` and ``agent.protocol``, deliberately not on the
workbench's legacy caller, so the agent core keeps exactly one transport and one wire parser.
The adapter owns the handshake sequence, the backend contract and the launch command of the
runtime it speaks to; the binary path, working directory and process environment are injected,
so nothing here depends on where the workbench keeps its files or how it names its concepts.

Scope of this step: bind, start (initialize plus a new session), status and stop. Turning text
into a turn (``send``) and aborting one (``cancel``) belong to later steps and raise until then.
"""
import os
import threading
from dataclasses import dataclass
from typing import Callable, Mapping

from . import protocol
from .backend import RuntimeUnavailable
from .runtime import Runtime

PROVIDER='reasonix'
PROTOCOL_VERSION=1
INITIALIZE_TIMEOUT_SECONDS=60.0
SESSION_TIMEOUT_SECONDS=90.0
CLOSE_TIMEOUT_SECONDS=5.0
DETAIL_LIMIT=200
INBOX_CAPACITY=50
LIFECYCLE_CAPACITY=50


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
    remote_session_id: str|None=None
    client_name: str='gewu-workbench'
    client_version: str='0'


def _detail(error):
    return ('%s: %s' % (type(error).__name__,error))[:DETAIL_LIMIT]


class ReasonixAdapter:
    """One adapter instance owns one runtime process and one remote session.

    It reports through an injected sink and never reads a stream itself: the runtime hands it
    parsed messages and lifecycle events, and translating those into agent output belongs to a
    later step. Start and stop are the only lifecycle moves implemented here.
    """

    def __init__(self,config,on_diagnostic=None):
        if not isinstance(config,ReasonixLaunchConfig):raise TypeError('需要 ReasonixLaunchConfig')
        if config.remote_session_id is not None:raise NotImplementedError('本步骤只创建新会话')
        if not config.executable:raise ValueError('缺少可执行文件')
        if not config.cwd:raise ValueError('缺少工作目录')
        # session/new carries this path to the peer, so it must be a real absolute directory
        # rather than something the peer would have to interpret relative to its own start-up.
        if not os.path.isabs(config.cwd):raise ValueError('工作目录必须是绝对路径：'+config.cwd)
        if not os.path.isdir(config.cwd):raise ValueError('工作目录不存在：'+config.cwd)
        self._config=config
        self._sink=lambda event:None
        self._on_diagnostic=on_diagnostic or (lambda kind,line:None)
        self._runtime=None
        self._remote_session_id=None
        self._handshake=None
        self._ready=False
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
                self._remote_session_id=self._create_session(runtime)
            except BaseException:
                self._teardown()
                raise
            self._ready=True

    def send(self,text:str) -> None:
        raise NotImplementedError('send 在后续步骤实现')

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
        if not session_id:raise RuntimeUnavailable('创建会话未返回会话标识')
        return str(session_id)

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
        """Inbound messages are recorded for now; translating them is a later step's job."""
        try:method=protocol.method_of(message)
        except Exception:method=None
        self._inbox.append(method or 'unparsed')
        del self._inbox[:-INBOX_CAPACITY]

    def _on_lifecycle(self,event):
        self._lifecycle.append(event.get('type'))
        del self._lifecycle[:-LIFECYCLE_CAPACITY]
