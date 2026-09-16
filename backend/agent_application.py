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
from .agent.bridge import AgentBridge,AgentBridgeError,SessionNotFound
from .agent.reasonix import ExecutionBudget,ReasonixAdapter,ReasonixLaunchConfig
from .runtime_isolation import environment

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
_SHUTTING_DOWN=False


class ApplicationStopping(AgentBridgeError):
    """The application is on its way out and no longer accepts new agent sessions."""


def backend_factory(dataset_id,model):
    """The real backend factory for one dataset.

    A single seam on purpose: replacing this is how a test drives the whole HTTP surface, the real
    bridge and the real event store against its own backend, without the HTTP layer learning
    anything about what a backend is made of.
    """
    def build():
        # An agent session clarifies through an interaction on its own session, so it is built with
        # no surface of its own for the retired persisted-question capability.
        cwd=harness.runtime_dir(dataset_id,'agent')
        return ReasonixAdapter(ReasonixLaunchConfig(executable=harness.executable(),model=model,
            cwd=str(cwd),env=environment(dataset_id,None,{}),runtime_key='agent:'+dataset_id,
            budget=UI_EXECUTION_BUDGET))
    return build


def _bridge(dataset_id,model):
    key=(dataset_id,model)
    with _LOCK:
        bridge=_BRIDGES.get(key)
        if bridge is None:
            bridge=AgentBridge(backend_factory(dataset_id,model),dataset_id=dataset_id)
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
        _BRIDGES.clear();_SESSIONS.clear()


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
        _SESSIONS.clear();_BRIDGES.clear()
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
    bridge=_bridge(dataset_id,model)
    session=bridge.create_session(provider='reasonix',model=model)
    with _LOCK:_SESSIONS[session['id']]=bridge
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
