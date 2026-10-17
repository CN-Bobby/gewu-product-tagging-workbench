// Agent panel state: one reducer for the whole session, so a turn's outcome, a tool's lifecycle and
// a question's answer all land in one place instead of scattered setState calls.
//
// Two statuses, never one loading flag: a cancelled turn and a stopped session are different facts,
// and so are a failed turn and a failed session. Nothing here knows which agent runtime is
// underneath; it consumes application events and application errors only.
//
// One turn renders one activity card, not one message per tool event: the event store keeps every
// tool event in full, and this reducer projects them onto a single node per turn so the transcript
// stays readable. The projection lives here; the copy lives in agentActivity.js.
//
// Pure module on purpose — no React import — so the whole state machine can be tested directly.

import {ACTIVITY_STATUS,secondsBetween} from './agentActivity.js';

export const SESSION_STATUS={IDLE:'idle',CREATING:'creating',CREATED:'created',STARTING:'starting',ACTIVE:'active',STOPPING:'stopping',STOPPED:'stopped',FAILED:'failed',UNAVAILABLE:'unavailable'};
export const TURN_STATUS={IDLE:'idle',SENDING:'sending',CANCELLING:'cancelling'};

export const PANEL={AGENT:'agent',LEGACY:'legacy'};
export const DEFAULT_PANEL=PANEL.AGENT;

export const initialAgentState={
  sessionStatus:SESSION_STATUS.IDLE,
  turnStatus:TURN_STATUS.IDLE,
  sessionId:null,
  remoteSessionId:null,
  backendAvailable:false,
  items:[],          // transcript, in event order: user text, one activity card per turn, assistant text
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

// --- turn activity ---------------------------------------------------------
//
// One card per turn. The card node is placed when the turn starts, so a later tool event only ever
// replaces fields of a node that is already there: the reply below it never moves.

function freshActivity(payload){
  return {
    turnId:(payload&&payload.message_id)||null,
    startedAt:(payload&&payload.timestamp)||null,
    settledAt:null,
    seconds:null,
    status:ACTIVITY_STATUS.RUNNING,
    currentTool:null,
    waiting:false,
    cancelling:false,
    resumeHint:false,
    tools:[],
    completedCount:0,
    warningCount:0,
    retryCount:0,
  };
}

// Completed and failed are derived from the tool list itself, so the counts can never drift from the
// timeline. The backend reports one host status per call, so warning and retry share a source: there
// is no separate retry signal to invent a second number from.
function withCounts(activity){
  const tools=activity.tools||[];
  return {...activity,
    completedCount:tools.filter(tool=>tool.status==='completed').length,
    warningCount:tools.filter(tool=>tool.status==='failed').length,
    retryCount:tools.filter(tool=>tool.status==='failed').length};
}

function lastActivityIndex(items){
  for(let index=items.length-1;index>=0;index-=1)if(items[index].kind==='activity')return index;
  return -1;
}

// Patch the newest activity card. A stream that opens on a tool event (a replay that starts mid-turn)
// still gets exactly one card instead of dropping the tool or inventing a card per call.
function patchActivity(state,mutate){
  const index=lastActivityIndex(state.items);
  if(index<0){
    const opened={kind:'activity',id:'activity:'+(state.lastSequence||0),...freshActivity(null)};
    return {...state,items:[...state.items,mutate(opened)]};
  }
  const items=state.items.map((item,at)=>at===index?mutate(item):item);
  return {...state,items};
}

function startTool(activity,payload){
  const id=payload.tool_call_id||null;
  // Without an identity the call cannot be tracked or settled, so it is not shown as a row.
  if(!id||(activity.tools||[]).some(tool=>tool.id===id))return activity;
  const tool={id,tool:payload.tool||null,status:'pending',startedAt:payload.timestamp||null,
              settledAt:null,duration:null,error:null,retry:false};
  return withCounts({...activity,currentTool:tool.tool,waiting:false,resumeHint:false,
                     tools:[...(activity.tools||[]),tool]});
}

function settleTool(activity,payload){
  const id=payload.tool_call_id||null;
  if(!id)return activity;
  const failed=payload.host_status!=='completed';
  let found=false;
  const tools=(activity.tools||[]).map(tool=>{
    if(tool.id!==id)return tool;
    found=true;
    return {...tool,status:failed?'failed':'completed',settledAt:payload.timestamp||null,
            duration:secondsBetween(tool.startedAt,payload.timestamp),error:payload.error||null,
            retry:failed};
  });
  if(!found)return activity;
  return withCounts({...activity,tools,currentTool:null});
}

// Closing is idempotent and never downgrades: a late `message_completed` may add the backend's own
// duration, but it cannot turn a cancelled turn back into a completed one.
function closeActivity(activity,status,payload){
  const settledAt=(payload&&payload.timestamp)||activity.settledAt||null;
  const measured=payload&&Number.isFinite(payload.seconds)?payload.seconds:null;
  const seconds=measured!==null?measured:(activity.seconds!==null&&activity.seconds!==undefined
    ?activity.seconds:secondsBetween(activity.startedAt,settledAt));
  const ranked={[ACTIVITY_STATUS.RUNNING]:0,[ACTIVITY_STATUS.DONE]:1,
                [ACTIVITY_STATUS.CANCELLED]:2,[ACTIVITY_STATUS.FAILED]:2};
  const next=(ranked[status]||0)>=(ranked[activity.status]||0)?status:activity.status;
  return withCounts({...activity,status:next,settledAt,seconds,cancelling:false,waiting:false,
                     currentTool:next===ACTIVITY_STATUS.RUNNING?activity.currentTool:null});
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
    case 'message_started':{
      // The card is placed before the reply it belongs to, so every later tool event replaces fields
      // of a node that is already in the transcript instead of inserting one.
      const card={kind:'activity',id:'activity:'+(payload.message_id||nextItemId(base,'activity')),
                  ...freshActivity(payload)};
      return appendAssistant({...base,items:[...base.items,card]},'',payload.message_id||null);
    }
    case 'assistant_text_delta':
      return appendAssistant(base,payload.text||'',payload.message_id||null);
    case 'turn_settled':
      return {...closeAssistant(patchActivity(base,activity=>closeActivity(activity,ACTIVITY_STATUS.DONE,payload)),'done'),
              turnStatus:TURN_STATUS.IDLE};
    case 'message_completed':
      return {...closeAssistant(patchActivity(base,activity=>closeActivity(activity,ACTIVITY_STATUS.DONE,payload)),'done'),
              turnStatus:TURN_STATUS.IDLE};
    case 'message_cancelled':
      return {...closeAssistant(patchActivity(base,activity=>closeActivity(activity,ACTIVITY_STATUS.CANCELLED,payload)),'cancelled'),
              turnStatus:TURN_STATUS.IDLE};
    case 'message_failed':
      return {...closeAssistant(patchActivity(base,activity=>closeActivity(activity,ACTIVITY_STATUS.FAILED,payload)),'failed'),
              turnStatus:TURN_STATUS.IDLE,
              error:{code:'turn_error',message:'本轮执行失败',fatal:payload.fatal===true}};
    case 'session_failed':
      return {...base,sessionStatus:SESSION_STATUS.FAILED,backendAvailable:false};
    case 'session_stopping':
      return {...base,sessionStatus:SESSION_STATUS.STOPPING};
    case 'session_stopped':
      return {...base,sessionStatus:SESSION_STATUS.STOPPED,backendAvailable:false};
    case 'tool_call_started':
      // A tool event never appends a chat node: it only updates the turn's card.
      return patchActivity(base,activity=>startTool(activity,payload));
    case 'tool_call_settled':
      return patchActivity(base,activity=>settleTool(activity,payload));
    case 'question_requested':
    case 'permission_requested':
      return patchActivity({...base,interactions:[...base.interactions,
              {kind:type==='question_requested'?'question':'permission',
               id:payload.interaction_id,interactionId:payload.interaction_id,status:'pending',
               question:payload.question||payload.title||'',multi:payload.multi===true,
               options:payload.options||[],toolCallId:payload.tool_call_id||null}]},
              activity=>({...activity,waiting:true,resumeHint:false}));
    case 'question_settled':
    case 'permission_settled':
      return patchActivity(settleInteraction(base,payload,payload.outcome==='selected'?'answered':'cancelled'),
              activity=>({...activity,waiting:false,resumeHint:true}));
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
      // The card says so immediately, and any event still in flight stays on that same card.
      return action.accepted
        ?{...patchActivity(state,activity=>({...activity,cancelling:true})),turnStatus:TURN_STATUS.CANCELLING}
        :state;
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
