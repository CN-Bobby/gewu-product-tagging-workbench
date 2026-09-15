"""HTTP surface for agent sessions: commands in, events out through the event store.

This layer knows sessions, messages, cancellation, stopping and application errors. It does not
know which agent runtime sits underneath, how it is addressed or how it is spoken to: every
operation is one call on the bridge, and every failure is one of the bridge's own typed errors
turned into an application code. Output deliberately does not travel here — it is already in the
event store, which the SSE endpoint streams — so there is exactly one event channel.

An application code never carries the underlying runtime's own text: a caller gets a stable code,
a safe message and whether the failure was fatal to the session.
"""
from fastapi import APIRouter,HTTPException

from . import agent_application
from .agent.backend import CancelledError,RuntimeUnavailable,TurnError
from .agent.bridge import (AgentBridgeError,BackendStartError,BackendStopError,InvalidSessionState,
    SessionBusy,SessionNotFound)

router=APIRouter(prefix='/api/agent')

# One place mapping the bridge's typed failures onto the application surface. The status codes stay
# deliberately coarse: a caller can act on "not found", "not now", "the turn failed" and "the
# runtime is gone" without learning anything about how the agent is run.
_ERROR_MAP={
    SessionNotFound:(404,'session_not_found'),
    SessionBusy:(409,'session_busy'),
    InvalidSessionState:(409,'session_state'),
    TurnError:(502,'turn_error'),
    CancelledError:(409,'cancelled'),
    RuntimeUnavailable:(503,'runtime_unavailable'),
    BackendStartError:(502,'backend_start_failed'),
    BackendStopError:(502,'backend_stop_failed'),
}


def _raise_application_error(error):
    status,code=_ERROR_MAP.get(type(error),(502,'bridge_error'))
    payload={'code':code,'message':str(error)}
    fatal=getattr(error,'fatal',None)
    if fatal is not None:payload['fatal']=bool(fatal)
    raise HTTPException(status_code=status,detail=payload) from error


def _call(operation):
    """Run one bridge call and translate only the failures the bridge declares. Anything else is a
    defect in this layer or below it and is left to surface as a server error."""
    try:return operation()
    except (AgentBridgeError,TurnError,CancelledError,RuntimeUnavailable) as error:
        _raise_application_error(error)


@router.post('/sessions')
def create_session(body:dict):
    return agent_application.create(body.get('dataset_id'),body.get('model'))


@router.post('/sessions/{session_id}/start')
def start_session(session_id:str):
    session=_call(lambda:agent_application.bridge_for(session_id).start(session_id))
    return {'session_id':session_id,'status':session['status'],
            'remote_session_id':session.get('remote_session_id')}


@router.post('/sessions/{session_id}/messages')
def send_message(session_id:str,body:dict):
    """Run one turn and report its outcome. The turn's own output is in the event store, so the
    response carries the result of the command and never a second copy of the stream."""
    text=body.get('text')
    if not isinstance(text,str) or not text.strip():raise ValueError('消息需要非空文本')
    stored=_call(lambda:agent_application.bridge_for(session_id).send(session_id,text))
    payload=stored.get('payload') or {}
    return {'session_id':session_id,'outcome':'completed','sequence':stored.get('sequence'),
            'message_id':payload.get('message_id'),'seconds':payload.get('seconds')}


@router.post('/sessions/{session_id}/cancel')
def cancel_session(session_id:str):
    """Ask for the turn in flight to be abandoned. A true flag means the request was accepted, not
    that the turn has stopped; the turn's own outcome arrives as events."""
    answer=_call(lambda:agent_application.bridge_for(session_id).cancel(session_id))
    return {'session_id':session_id,'cancel_requested':bool(answer.get('cancelled'))}


@router.post('/sessions/{session_id}/stop')
def stop_session(session_id:str):
    session=_call(lambda:agent_application.bridge_for(session_id).stop(session_id))
    return {'session_id':session_id,'status':session['status']}


@router.get('/sessions/{session_id}')
def session_status(session_id:str):
    return _call(lambda:agent_application.status(session_id))


# --- interactions ----------------------------------------------------------
#
# Pure forwarding, added because a panel has to answer a question to get past it. The caller
# addresses an interaction by the identity the event carried; the label a user sees is never sent
# back, because the peer only ever offered option ids.

def _interaction_answer(session_id,interaction_id,body,method):
    # The bridge lookup happens inside the call so an unknown session is translated like any other
    # application failure instead of escaping as an unhandled error.
    if method.startswith('answer'):
        option_id=(body or {}).get('option_id')
        if not isinstance(option_id,str) or not option_id.strip():raise ValueError('回答需要 option_id')
        accepted=_call(lambda:getattr(agent_application.bridge_for(session_id),method)
                       (session_id,interaction_id,option_id))
    else:
        accepted=_call(lambda:getattr(agent_application.bridge_for(session_id),method)
                       (session_id,interaction_id))
    # The flag says the answer was accepted and handed to the peer. It does not say the interaction
    # is settled: that arrives as the settlement event.
    return {'session_id':session_id,'interaction_id':interaction_id,'accepted':bool(accepted)}


@router.post('/sessions/{session_id}/questions/{interaction_id}/answer')
def answer_question(session_id:str,interaction_id:str,body:dict):
    return _interaction_answer(session_id,interaction_id,body,'answer_question')


@router.post('/sessions/{session_id}/questions/{interaction_id}/cancel')
def cancel_question(session_id:str,interaction_id:str):
    return _interaction_answer(session_id,interaction_id,None,'cancel_question')


@router.post('/sessions/{session_id}/permissions/{interaction_id}/answer')
def answer_permission(session_id:str,interaction_id:str,body:dict):
    return _interaction_answer(session_id,interaction_id,body,'answer_permission')


@router.post('/sessions/{session_id}/permissions/{interaction_id}/cancel')
def cancel_permission(session_id:str,interaction_id:str):
    return _interaction_answer(session_id,interaction_id,None,'cancel_permission')
