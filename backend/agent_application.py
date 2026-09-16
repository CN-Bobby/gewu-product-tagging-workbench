"""Composition root for the agent application surface.

The HTTP layer never decides which runtime sits under a bridge; this module does, and it is the
only new place allowed to know. One bridge per dataset, because the backend factory is bound to a
per-dataset runtime directory: the agent session gets the same working directory, system prompt,
tool surface and isolated provider environment the workbench already builds for its own runtimes,
and no wider one.

Sessions are tracked here because a bridge owns the backends: an operation on a session has to
reach the bridge that created it, and nothing outside this module needs to know which one that is.
That registry is also what makes shutdown possible: the application owns which runtimes are live,
so closing them on the way out belongs here and not in the runtime or the HTTP layer.
"""
import threading
import time

from . import harness
from .agent import events as store
from .agent.bridge import (AgentBridge,AgentBridgeError,STATUS_FAILED,STATUS_STOPPED,SessionNotFound)
from .agent.reasonix import ExecutionBudget,ReasonixAdapter,ReasonixLaunchConfig
from .runtime_isolation import credential_selection,environment

# One app lifetime closes every session it owns before the process ends. Each close is already
# bounded inside the backend (close timeout, then terminate, then kill), so this budget only has to
# cover the number of sessions, not an unbounded wait on any one of them.
SHUTDOWN_TIMEOUT_SECONDS=15.0

# The interactive budget. A person is usually watching, so an unproductive turn is ended sooner than
# a batch one; waiting on a question pauses the idle deadline and never the wall-clock one, and the
# wall clock stays at the transport bound the workbench has always used.
UI_EXECUTION_BUDGET=ExecutionBudget(first_activity_timeout=120,idle_timeout=180,
                                    tool_wait_timeout=360,turn_wall_clock_timeout=1200)

_LOCK=threading.Lock()
_BRIDGES={}
_SESSIONS={}
# The runtime identity each session was created with: (model, provider, credential). Held here
# because a bridge is shared by every session with the same identity, so the bridge itself cannot
# answer "what did *this* session freeze".
_IDENTITIES={}
_SHUTTING_DOWN=False


class ApplicationStopping(AgentBridgeError):
    """The application is on its way out and no longer accepts new agent sessions."""


class RuntimeConfigInUse(AgentBridgeError):
    """The dataset already has a live session frozen to a different runtime configuration.

    Every session of one dataset shares one Reasonix home, so a second session on a different
    provider or model would rewrite the configuration the first one is still running on. Refusing is
    the honest answer: the alternative is a live session whose recovery reads someone else's model.
    """


def runtime_identity(model,provider,credential_id):
    """The frozen runtime configuration of one session, as one comparable value."""
    return (model,provider,credential_id or '')


def active_sessions():
    """Sessions this process owns that have not reached a terminal state.

    Read-only and cheap: it reports the durable state of sessions the process still holds, and it
    never builds a runtime, writes a row or changes a session. A session that exists only as a
    durable row is not live here, because nothing in this process can launch or recover it.
    """
    with _LOCK:items=list(_SESSIONS.items())
    live=[]
    for session_id,bridge in items:
        row=store.get_session(session_id)
        if row is None or row['status'] in (STATUS_STOPPED,STATUS_FAILED):continue
        live.append({'session_id':session_id,'dataset_id':row['dataset_id'],'model':row['model'],
                     'identity':_IDENTITIES.get(session_id),'bridge':bridge})
    return live


def has_active_sessions():
    """Whether any agent session of this process is still live. The settings lock asks this."""
    return bool(active_sessions())


def _assert_runtime_config_free(dataset_id,identity):
    for session in active_sessions():
        if session['dataset_id']!=dataset_id:continue
        if session['identity']==identity:continue
        raise RuntimeConfigInUse('该表格已有使用其它模型配置的活动会话，请先停止后再创建新的会话')


def backend_factory(dataset_id,model,credentials=None):
    """The real backend factory for one dataset.

    A single seam on purpose: replacing this is how a test drives the whole HTTP surface, the real
    bridge and the real event store against its own backend, without the HTTP layer learning
    anything about what a backend is made of.

    ``credentials`` is the selection the session froze at creation, not a lookup: the runtime is
    launched with the key its session was created with, for its whole life including recovery.
    """
    selection=dict(credentials or {})
    def build():
        # An agent session clarifies through an interaction on its own session, so it is built with
        # no surface of its own for the retired persisted-question capability.
        cwd=harness.runtime_dir(dataset_id,'agent')
        return ReasonixAdapter(ReasonixLaunchConfig(executable=harness.executable(),model=model,
            cwd=str(cwd),env=environment(dataset_id,None,selection),runtime_key='agent:'+dataset_id,
            budget=UI_EXECUTION_BUDGET))
    return build


def _bridge(dataset_id,identity):
    """The bridge for one frozen runtime configuration.

    Keyed by the configuration and not just the dataset: a bridge owns the backend factory, so
    reusing one across two different credentials would hand a new session the previous session's key.
    """
    model,provider,credential_id=identity
    key=(dataset_id,model,credential_id)
    with _LOCK:
        bridge=_BRIDGES.get(key)
        if bridge is None:
            credentials={provider:credential_id} if credential_id else {}
            bridge=AgentBridge(backend_factory(dataset_id,model,credentials),dataset_id=dataset_id)
            _BRIDGES[key]=bridge
        return bridge


def start():
    """Arm the surface for one application lifetime.

    A lifetime begins with nothing registered and the gate open. Re-entering the lifespan in the
    same process (a reload, or a test that starts the app again) must not inherit the previous
    lifetime's sessions or its closed gate.
    """
    global _SHUTTING_DOWN
    with _LOCK:
        _SHUTTING_DOWN=False
        _BRIDGES.clear();_SESSIONS.clear();_IDENTITIES.clear()


def shutdown(timeout=SHUTDOWN_TIMEOUT_SECONDS):
    """Close every live agent session this process owns, then refuse new ones.

    Called first on application shutdown, before the background jobs: an agent session is the
    interactive surface, and closing it stops a turn from starting work while the rest of the
    application is already going away. One session that will not close must not hold up the others,
    and the runtime's job object stays the last resort for a process that ignores the close.
    """
    global _SHUTTING_DOWN
    with _LOCK:
        _SHUTTING_DOWN=True
        sessions=list(_SESSIONS.items())
        # Cleared before closing, so a second call is a no-op and nothing can be re-registered in
        # the middle of a close.
        _SESSIONS.clear();_BRIDGES.clear();_IDENTITIES.clear()
    stopped=[];failures=[]
    # Every close is put in flight before any of them is waited on. Waiting as we go would let one
    # session that never returns consume the whole budget, so the sessions behind it would never
    # even be asked to stop; this way the budget is only spent on waiting, never on starting.
    closing=[]
    for session_id,bridge in sessions:
        outcome={}
        def close(session_id=session_id,bridge=bridge,outcome=outcome):
            try:bridge.stop(session_id);outcome['stopped']=True
            except Exception as error:
                outcome['error']='%s: %s'%(type(error).__name__,str(error)[:200])
        worker=threading.Thread(target=close,name='agent-shutdown',daemon=True)
        worker.start()
        closing.append((session_id,worker,outcome))
    deadline=time.monotonic()+max(0.0,float(timeout))
    for session_id,worker,outcome in closing:
        worker.join(max(0.0,deadline-time.monotonic()))
        if worker.is_alive():
            failures.append({'session_id':session_id,'error':'session did not close in time'})
        elif outcome.get('stopped'):
            stopped.append(session_id)
        else:
            failures.append({'session_id':session_id,'error':outcome.get('error') or 'close produced no outcome'})
    return {'stopped':stopped,'failures':failures}


def create(dataset_id,model=None):
    if not isinstance(dataset_id,str) or not dataset_id.strip():
        raise ValueError('创建 agent 会话需要 dataset_id')
    with _LOCK:
        if _SHUTTING_DOWN:raise ApplicationStopping('应用正在关闭，无法创建新的 Agent 会话')
    model=model or harness.DEFAULT_MODEL
    # Model, provider and credential are resolved together, once, before the session row exists.
    # From here on the session carries this configuration: a later settings change cannot move a live
    # session onto another key, and a recovery resumes with the key the session was created with.
    provider,credential_id=credential_selection(model)
    identity=runtime_identity(model,provider,credential_id)
    _assert_runtime_config_free(dataset_id,identity)
    bridge=_bridge(dataset_id,identity)
    session=bridge.create_session(provider='reasonix',model=model)
    with _LOCK:
        _SESSIONS[session['id']]=bridge
        _IDENTITIES[session['id']]=identity
    return session


def bridge_for(session_id):
    """The bridge that owns this session, or the application's own not-found error."""
    with _LOCK:bridge=_SESSIONS.get(session_id)
    if bridge is None:raise SessionNotFound('Agent 会话不存在：'+str(session_id))
    return bridge


def status(session_id):
    """What the application knows about one session.

    A session this process did not create still has a durable row, so its recorded state is
    reported with an empty backend rather than pretending the session does not exist.
    """
    with _LOCK:bridge=_SESSIONS.get(session_id)
    if bridge is not None:return bridge.status(session_id)
    row=store.get_session(session_id)
    if row is None:raise SessionNotFound('Agent 会话不存在：'+str(session_id))
    return {'session_id':session_id,'status':row['status'],
            'remote_session_id':row.get('remote_session_id'),'backend':None}
