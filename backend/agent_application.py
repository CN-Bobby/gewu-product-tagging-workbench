"""Composition root for the agent application surface.

The HTTP layer never decides which runtime sits under a bridge; this module does, and it is the
only new place allowed to know. One bridge per dataset, because the backend factory is bound to a
per-dataset runtime directory: the agent session gets the same working directory, system prompt,
tool surface and isolated provider environment the workbench already builds for its own runtimes,
and no wider one.

Sessions are tracked here because a bridge owns the backends: an operation on a session has to
reach the bridge that created it, and nothing outside this module needs to know which one that is.
"""
import threading

from . import harness
from .agent import events as store
from .agent.bridge import AgentBridge,SessionNotFound
from .agent.reasonix import ReasonixAdapter,ReasonixLaunchConfig
from .runtime_isolation import environment

_LOCK=threading.Lock()
_BRIDGES={}
_SESSIONS={}


def backend_factory(dataset_id,model):
    """The real backend factory for one dataset.

    A single seam on purpose: replacing this is how a test drives the whole HTTP surface, the real
    bridge and the real event store against its own backend, without the HTTP layer learning
    anything about what a backend is made of.
    """
    def build():
        cwd=harness.runtime_dir(dataset_id,'agent')
        return ReasonixAdapter(ReasonixLaunchConfig(executable=harness.executable(),model=model,
            cwd=str(cwd),env=environment(dataset_id,None,{}),runtime_key='agent:'+dataset_id))
    return build


def _bridge(dataset_id,model):
    key=(dataset_id,model)
    with _LOCK:
        bridge=_BRIDGES.get(key)
        if bridge is None:
            bridge=AgentBridge(backend_factory(dataset_id,model),dataset_id=dataset_id)
            _BRIDGES[key]=bridge
        return bridge


def create(dataset_id,model=None):
    if not isinstance(dataset_id,str) or not dataset_id.strip():
        raise ValueError('创建 agent 会话需要 dataset_id')
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
