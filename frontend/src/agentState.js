// Agent panel state: one reducer for the whole session, so a turn's outcome, a tool's lifecycle and
// a question's answer all land in one place instead of scattered setState calls.
//
// Two statuses, never one loading flag: a cancelled turn and a stopped session are different facts,
// and so are a failed turn and a failed session. Nothing here knows which agent runtime is
// underneath; it consumes application events and application errors only.
//
// Pure module on purpose — no React import — so the whole state machine can be tested directly.

export const SESSION_STATUS={IDLE:'idle',CREATING:'creating',CREATED:'created',STARTING:'starting',ACTIVE:'active',STOPPING:'stopping',STOPPED:'stopped',FAILED:'failed',UNAVAILABLE:'unavailable'};
export const TURN_STATUS={IDLE:'idle',SENDING:'sending',CANCELLING:'cancelling'};

export const initialAgentState={
  sessionStatus:SESSION_STATUS.IDLE,
  turnStatus:TURN_STATUS.IDLE,
  sessionId:null,
  remoteSessionId:null,
  backendAvailable:false,
  items:[],          // transcript, in event order: user text, assistant text, tool lifecycles
  interactions:[],   // questions and permission requests waiting for, or settled by, the user
  error:null,
  lastSequence:0,
};

// A turn only accepts new input when the session is usable and nothing is in flight. This is the
// one place that answers "is the composer enabled", so no component has to reason about it.
export const canSend=state=>state.sessionStatus===SESSION_STATUS.ACTIVE&&state.turnStatus===TURN_STATUS.IDLE;
export const pendingInteractions=state=>state.interactions.filter(item=>item.status==='pending');

const nextItemId=(state,prefix)=>prefix+':'+(state.lastSequence||0)+':'+state.items.length;

function appendAssistant(state,text,messageId){
  const items=state.items.slice();
  for(let index=items.length-1;index>=0;index-=1){
    const item=items[index];
    if(item.kind!=='assistant'||item.status!=='streaming')continue;
    if(messageId&&item.messageId&&item.messageId!==messageId)continue;
    items[index]={...item,text:item.text+text,messageId:item.messageId||messageId||null};
    return {...state,items};
  }
  return {...state,items:[...items,{kind:'assistant',id:nextItemId(state,'assistant'),text,status:'streaming',messageId:messageId||null}]};
}

function closeAssistant(state,status){
  let touched=false;
  const items=state.items.map(item=>{
    if(item.kind!=='assistant'||item.status!=='streaming')return item;
    touched=true;return {...item,status};
  });
  return touched?{...state,items}:state;
}

function settleTool(state,payload,hostStatus){
  const items=state.items.map(item=>item.kind==='tool'&&item.toolCallId===payload.tool_call_id
    ?{...item,hostStatus:hostStatus||item.hostStatus}:item);
  return {...state,items};
}

function settleInteraction(state,payload,status){
  const interactions=state.interactions.map(item=>item.interactionId===payload.interaction_id
    ?{...item,status,selectedOptionId:payload.option_id||null}:item);
  return {...state,interactions};
}

function applyAgentEvent(state,event){
  const type=event.event_type,payload=event.payload||{};
  const sequence=Number.isFinite(event.sequence)?event.sequence:null;
  // Sequence is the only authoritative order. A replayed or re-delivered event carries a sequence
  // the reducer has already seen, so it is dropped instead of duplicating a message or a tool.
  if(sequence!==null&&sequence<=state.lastSequence)return state;
  const base=sequence===null?state:{...state,lastSequence:sequence};
  switch(type){
    case 'message_started':
      return appendAssistant(base,'',payload.message_id||null);
    case 'assistant_text_delta':
      return appendAssistant(base,payload.text||'',payload.message_id||null);
    case 'turn_settled':
      return {...closeAssistant(base,'done'),turnStatus:TURN_STATUS.IDLE};
    case 'message_completed':
      return {...closeAssistant(base,'done'),turnStatus:TURN_STATUS.IDLE};
    case 'message_cancelled':
      return {...closeAssistant(base,'cancelled'),turnStatus:TURN_STATUS.IDLE};
    case 'message_failed':
      return {...closeAssistant(base,'failed'),turnStatus:TURN_STATUS.IDLE,
              error:{code:'turn_error',message:'本轮执行失败',fatal:payload.fatal===true}};
    case 'session_failed':
      return {...base,sessionStatus:SESSION_STATUS.FAILED,backendAvailable:false};
    case 'session_stopping':
      return {...base,sessionStatus:SESSION_STATUS.STOPPING};
    case 'session_stopped':
      return {...base,sessionStatus:SESSION_STATUS.STOPPED,backendAvailable:false};
    case 'tool_call_started':
      return {...base,items:[...base.items,{kind:'tool',id:payload.tool_call_id||nextItemId(base,'tool'),
              toolCallId:payload.tool_call_id||null,server:payload.server||null,tool:payload.tool||null,
              hostStatus:payload.host_status||'pending'}]};
    case 'tool_call_settled':
      return settleTool(base,payload,payload.host_status);
    case 'question_requested':
    case 'permission_requested':
      return {...base,interactions:[...base.interactions,{kind:type==='question_requested'?'question':'permission',
              id:payload.interaction_id,interactionId:payload.interaction_id,status:'pending',
              question:payload.question||payload.title||'',multi:payload.multi===true,
              options:payload.options||[],toolCallId:payload.tool_call_id||null}]};
    case 'question_settled':
    case 'permission_settled':
      return settleInteraction(base,payload,payload.outcome==='selected'?'answered':'cancelled');
    default:
      return base;
  }
}

export function agentReducer(state,action){
  switch(action.type){
    case 'session_creating':
      return {...initialAgentState,sessionStatus:SESSION_STATUS.CREATING};
    case 'session_created':
      return {...state,sessionStatus:SESSION_STATUS.STARTING,sessionId:action.sessionId};
    case 'session_starting':
      return {...state,sessionStatus:SESSION_STATUS.STARTING};
    case 'session_started':
      return {...state,sessionStatus:SESSION_STATUS.ACTIVE,sessionId:action.sessionId||state.sessionId,
              remoteSessionId:action.remoteSessionId||null,backendAvailable:action.backendAvailable!==false,
              error:null};
    case 'session_snapshot':
      return snapshot(state,action.snapshot);
    case 'turn_sending':
      return {...state,turnStatus:TURN_STATUS.SENDING,error:null,
              items:[...state.items,{kind:'user',id:'user:'+state.items.length,text:action.text,status:'sent'}]};
    case 'turn_accepted':
      return {...state,turnStatus:TURN_STATUS.IDLE};
    case 'cancel_requested':
      // Accepted means asked for, not stopped: the turn stays open until its terminal event lands.
      return action.accepted?{...state,turnStatus:TURN_STATUS.CANCELLING}:state;
    case 'stop_requested':
      return {...state,sessionStatus:SESSION_STATUS.STOPPING};
    case 'stop_settled':
      return {...state,sessionStatus:SESSION_STATUS.STOPPED,backendAvailable:false};
    case 'interaction_answered':
      return settleInteraction(state,{interaction_id:action.interactionId},
        action.outcome==='selected'?'answered':'cancelled');
    case 'command_failed':
      return commandFailed(state,action.error,action.scope||'turn');
    case 'agent_event':
      return applyAgentEvent(state,action.event);
    case 'reset':
      return {...initialAgentState};
    default:
      return state;
  }
}

function snapshot(state,view){
  if(!view)return state;
  const status=view.status;
  // A session row can outlive the process that owned it. Without a backend nothing can be sent to
  // it, so it is reported as unavailable instead of being shown as a live conversation.
  const stale=status==='active'&&!view.backend;
  return {...state,sessionId:view.session_id||state.sessionId,
          remoteSessionId:view.remote_session_id||null,
          backendAvailable:!stale&&!!view.backend,
          sessionStatus:stale?SESSION_STATUS.UNAVAILABLE:SESSION_STATUS[String(status||'').toUpperCase()]||state.sessionStatus};
}

function commandFailed(state,error,scope){
  const next={...state,error:error||null};
  if(scope==='turn'){
    // A failed command is a failed turn. The session is only finished if the failure said so.
    return error&&error.fatal
      ?{...closeAssistant(next,'failed'),turnStatus:TURN_STATUS.IDLE,sessionStatus:SESSION_STATUS.FAILED,backendAvailable:false}
      :{...closeAssistant(next,'failed'),turnStatus:TURN_STATUS.IDLE};
  }
  return error&&error.fatal?{...next,sessionStatus:SESSION_STATUS.FAILED,backendAvailable:false}:next;
}
