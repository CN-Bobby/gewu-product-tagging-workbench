// Keyword research state: the form, the request body built from it, and one run's event projection.
//
// Pure module on purpose — no React import — so the ASIN parsing, the request body and the event
// projection can be tested directly, without a DOM and without a server. The site list and the
// node_top_n options are not here: they come from /meta, because the backend whitelist is the only
// source of that truth.
//
// The run projection is driven by the event stream and never by a polling loop: the snapshot from
// GET /runs/{id} is authoritative, and every streamed event is folded in by sequence.

export const PAGE={HOME:'home',TAGGING:'tagging',KEYWORD:'keyword',SETTINGS:'settings'};
export const PAGE_HOME=PAGE.HOME;

const ASIN_PATTERN=/^B0[A-Z0-9]{8}$/;
// Same separators the backend's extract_asins splits on: whitespace, comma, semicolon, 顿号.
const SEPARATORS=/[\s,;，；、]+/;

function splitTokens(text){
  return String(text==null?'':text).split(SEPARATORS).map(token=>token.trim()).filter(Boolean);
}

// Returns {valid,invalid} instead of silently dropping the rest: what was ignored has to be shown,
// because "I pasted 20 ASINs and 3 were used" is not something the user can discover otherwise.
// Duplicates are reported once on either side.
export function parseAsins(text){
  const valid=[],invalid=[],seenValid=new Set(),seenInvalid=new Set();
  for(const token of splitTokens(text)){
    const asin=token.toUpperCase();
    if(ASIN_PATTERN.test(asin)){
      if(seenValid.has(asin))continue;
      seenValid.add(asin);valid.push(asin);
    }else{
      if(seenInvalid.has(asin))continue;
      seenInvalid.add(asin);invalid.push(asin);
    }
  }
  return {valid,invalid};
}

export function parseNodeIds(text){
  const nodes=[],seen=new Set();
  for(const token of splitTokens(text)){
    if(seen.has(token))continue;
    seen.add(token);nodes.push(token);
  }
  return nodes;
}

function monthOf(value){
  const text=String(value==null?'':value).trim();
  return text||null;
}

export const initialKeywordForm={
  groups:[{key:'g1',marketplace:'',asinsText:'',nodeIdsText:'',nodeTopN:50}],
  dataMonth:'',
  matrixMonth:'',
  matrixEnabled:true,
  planHash:'',
};

// The body both /plan and /runs take: the form as the backend validates it. plan_hash is included
// only once a budget has been counted, which is what makes the confirmation binding.
export function buildRunRequest(form){
  const source=form||initialKeywordForm;
  const body={
    groups:(source.groups||[]).map((group,index)=>({
      marketplace:String(group.marketplace||'').trim().toUpperCase(),
      asins:parseAsins(group.asinsText).valid,
      node_ids:parseNodeIds(group.nodeIdsText),
      node_top_n:Number(group.nodeTopN)||50,
    })).filter(group=>group.marketplace||group.asins.length||group.node_ids.length),
    data_month:monthOf(source.dataMonth),
    matrix_month:source.matrixEnabled===false?null:monthOf(source.matrixMonth),
    matrix_enabled:source.matrixEnabled!==false,
  };
  const hash=String(source.planHash||'').trim();
  if(hash)body.plan_hash=hash;
  return body;
}

// --- run vocabulary ---------------------------------------------------------

export const RUN_STATUS={
  QUEUED:'queued',RUNNING:'running',CANCELLING:'cancelling',DONE:'done',
  FAILED:'failed',CANCELLED:'cancelled',INTERRUPTED:'interrupted',SKIPPED:'skipped',
};

export const ACTIVE_RUN_STATUSES=[RUN_STATUS.QUEUED,RUN_STATUS.RUNNING,RUN_STATUS.CANCELLING];
export const TERMINAL_RUN_STATUSES=[RUN_STATUS.DONE,RUN_STATUS.FAILED,RUN_STATUS.CANCELLED,RUN_STATUS.INTERRUPTED,RUN_STATUS.SKIPPED];
export const isActiveStatus=status=>ACTIVE_RUN_STATUSES.includes(status);
export const isTerminalStatus=status=>TERMINAL_RUN_STATUSES.includes(status);

// The frame name and the event types, named here once so both sides of the wire can be compared
// without a browser: a named SSE event never reaches EventSource.onmessage, and a mismatch fails
// silently instead of erroring.
export const SSE_EVENT_NAME='run_event';
export const RUN_EVENT={
  STARTED:'run_started',
  STAGE_STARTED:'stage_started',
  STAGE_PROGRESS:'stage_progress',
  STAGE_FINISHED:'stage_finished',
  LOG:'log',
  ARTIFACT_READY:'artifact_ready',
  FINISHED:'run_finished',
  FAILED:'run_failed',
  CANCELLED:'run_cancelled',
  SKIPPED:'run_skipped',
};
export const RUN_EVENT_TYPES=Object.values(RUN_EVENT);
export const TERMINAL_EVENT_TYPES=[RUN_EVENT.FINISHED,RUN_EVENT.FAILED,RUN_EVENT.CANCELLED,RUN_EVENT.SKIPPED];
export const isTerminalEvent=eventType=>TERMINAL_EVENT_TYPES.includes(eventType);

// Every path this page calls, in one place, so a route rename breaks in one file.
export const API_PREFIX='/api/keyword-research';
export const ENDPOINTS={
  meta:()=>`${API_PREFIX}/meta`,
  credentials:()=>`${API_PREFIX}/credentials`,
  plan:()=>`${API_PREFIX}/plan`,
  runs:()=>`${API_PREFIX}/runs`,
  run:runId=>`${API_PREFIX}/runs/${runId}`,
  runEvents:(runId,afterSequence)=>`${API_PREFIX}/runs/${runId}/events?after_sequence=${afterSequence}`,
  runCancel:runId=>`${API_PREFIX}/runs/${runId}/cancel`,
  runArtifact:(runId,name)=>`${API_PREFIX}/runs/${runId}/artifacts/${encodeURIComponent(name)}`,
  queue:()=>`${API_PREFIX}/queue`,
  queueOrder:()=>`${API_PREFIX}/queue/order`,
  queueStop:()=>`${API_PREFIX}/queue/stop`,
  queueContinue:()=>`${API_PREFIX}/queue/continue`,
  runHide:runId=>`${API_PREFIX}/runs/${runId}/hide`,
  runUnhide:runId=>`${API_PREFIX}/runs/${runId}/unhide`,
  runDirectory:runId=>`${API_PREFIX}/runs/${runId}/open-directory`,
};

// --- the run projection -----------------------------------------------------

export const MAX_LOG_LINES=Infinity;

export const initialKeywordState={
  runId:null,
  status:'idle',
  stage:'',
  message:'',
  error:'',
  plannedCalls:null,
  progress:null,      // {stage,done,total,unit,message} of the stage currently running
  artifacts:[],       // {name,bytes,sha256}
  log:[],             // {sequence,line}, oldest first, capped
  timeline:[],
  recentActivity:[],
  attempt:null,
  queuePosition:null,
  lastActivity:null,
  lastSequence:0,
  finished:false,
};

function artifactEntries(items){
  const source=items==null?[]:(Array.isArray(items)?items:[items]);
  const entries=[];
  for(const item of source){
    if(typeof item==='string'){entries.push({name:item,bytes:null,sha256:''});continue}
    if(item&&typeof item==='object'&&item.name)
      entries.push({name:String(item.name),bytes:item.bytes==null?null:Number(item.bytes),sha256:String(item.sha256||'')});
  }
  return entries;
}

// Artifacts are keyed by name so the ready frame and the finished frame describe one file, not two.
function mergeArtifacts(existing,incoming){
  const items=existing.slice();
  for(const entry of artifactEntries(incoming)){
    const index=items.findIndex(item=>item.name===entry.name);
    if(index<0){items.push(entry);continue}
    items[index]={...items[index],bytes:entry.bytes==null?items[index].bytes:entry.bytes,
      sha256:entry.sha256||items[index].sha256};
  }
  return items;
}

// One event folded in. A frame whose sequence is not newer than the last one is dropped: a
// reconnect replays from the cursor the client asked for, and a replayed stage line must not be
// printed twice or move progress backwards.
export function applyRunEvent(state,event){
  if(!event||typeof event!=='object')return state;
  const sequence=Number(event.sequence);
  if(!Number.isFinite(sequence)||sequence<=state.lastSequence)return state;
  const eventType=String(event.event_type||'');
  const payload=event.payload&&typeof event.payload==='object'?event.payload:{};
  const next={...state,lastSequence:sequence,runId:state.runId||event.run_id||null,
    recentActivity:state.recentActivity.concat([event]).slice(-20)};
  switch(eventType){
    case RUN_EVENT.STARTED:{
      const planned=Number(payload.planned_calls);
      return {...next,status:RUN_STATUS.RUNNING,message:'',
        plannedCalls:Number.isFinite(planned)?planned:state.plannedCalls};
    }
    case RUN_EVENT.STAGE_STARTED:
      return {...next,status:RUN_STATUS.RUNNING,stage:String(payload.stage||''),
        message:String(payload.message||''),progress:null,
        timeline:state.timeline.concat([{stage:String(payload.stage||''),status:'running',at:event.at||null,message:String(payload.message||'')}])};
    case RUN_EVENT.STAGE_PROGRESS:{
      const stage=String(payload.stage||state.stage);
      const done=payload.done==null?null:Number(payload.done),total=payload.total==null?null:Number(payload.total);
      const previous=state.progress&&state.progress.stage===stage?state.progress.done:null;
      return {...next,stage,status:RUN_STATUS.RUNNING,progress:{
        stage,
        done:done!==null&&Number.isFinite(done)?Math.max(previous??0,done):previous,
        total:total!==null&&Number.isFinite(total)?total:(payload.total==null?null:(state.progress?state.progress.total:null)),
        unit:String(payload.unit||''),
        message:String(payload.message||''),
      }};
    }
    case RUN_EVENT.STAGE_FINISHED:{
      const stage=String(payload.stage||state.stage),timeline=state.timeline.slice(),index=timeline.map(item=>item.stage).lastIndexOf(stage);
      if(index>=0)timeline[index]={...timeline[index],status:'done',finished_at:event.at||null};
      else timeline.push({stage,status:'done',finished_at:event.at||null});
      return {...next,stage,timeline};}
    case RUN_EVENT.LOG:{
      const log=state.log.concat([{sequence,line:String(payload.line==null?'':payload.line)}]);
      return {...next,log};
    }
    case RUN_EVENT.ARTIFACT_READY:
      return {...next,artifacts:mergeArtifacts(state.artifacts,payload)};
    case RUN_EVENT.FINISHED:
      return {...next,status:RUN_STATUS.DONE,message:'任务完成',finished:true,
        artifacts:mergeArtifacts(state.artifacts,payload.artifacts)};
    case RUN_EVENT.FAILED:
      return {...next,status:RUN_STATUS.FAILED,finished:true,error:String(payload.error||'运行失败')};
    case RUN_EVENT.CANCELLED:
      return {...next,status:RUN_STATUS.CANCELLED,message:'已取消',finished:true};
    case RUN_EVENT.SKIPPED:
      return {...next,status:RUN_STATUS.SKIPPED,message:'已跳过',finished:true};
    default:
      // An event type this build does not know still advances the cursor; the status stays as the
      // snapshot reported it rather than being invented from the payload.
      return next;
  }
}

// The snapshot is authoritative: it can say interrupted after a restart, which no event will ever
// report. Switching to another run resets the cursor and the log, or the new run's first frames
// would be dropped as replays.
export function applySnapshot(state,snapshot){
  if(!snapshot||typeof snapshot!=='object')return state;
  const runId=snapshot.run_id||state.runId;
  const switched=runId!==state.runId;
  const status=String(snapshot.status||state.status);
  const snapshotLog=Array.isArray(snapshot.log)?snapshot.log:Array.isArray(snapshot.logs)?snapshot.logs:[];
  const log=snapshotLog.map((item,index)=>typeof item==='string'?{sequence:index+1,line:item}:{sequence:Number(item.sequence)||index+1,line:String(item.line??item.message??'')});
  const timeline=Array.isArray(snapshot.timeline)?snapshot.timeline.map(item=>{
    if(item.stage)return item;
    const payload=item.payload&&typeof item.payload==='object'?item.payload:{};
    const kind=String(item.event_type||'event');
    return {stage:String(payload.stage||kind.replaceAll('_',' ')),status:kind==='stage_started'?'running':kind==='stage_finished'?'done':kind,message:String(payload.message||payload.line||''),at:item.at||null,sequence:item.sequence};
  }):null;
  return {...state,
    runId,
    status,
    stage:String(snapshot.stage==null?'':snapshot.stage),
    message:String(snapshot.message==null?'':snapshot.message),
    error:String(snapshot.error==null?'':snapshot.error),
    artifacts:mergeArtifacts([],snapshot.artifacts||[]),
    timeline:timeline|| (switched?[]:state.timeline),
    recentActivity:Array.isArray(snapshot.recent_activity)?snapshot.recent_activity.slice(-20):switched?[]:state.recentActivity,
    log:log.length?log:switched?[]:state.log,
    lastSequence:Math.max(switched?0:state.lastSequence,Number(snapshot.latest_sequence??snapshot.last_sequence)||0),
    progress:snapshot.progress&&typeof snapshot.progress==='object'?{stage:String(snapshot.progress.stage||snapshot.stage||''),done:snapshot.progress.done==null?null:Number(snapshot.progress.done),total:snapshot.progress.total==null?null:Number(snapshot.progress.total),unit:String(snapshot.progress.unit||''),message:String(snapshot.progress.message||'')}:null,
    attempt:snapshot.attempt??null,
    queuePosition:snapshot.queue_position??null,
    plannedCalls:snapshot.planned_calls??state.plannedCalls,
    lastActivity:snapshot.last_activity||snapshot.recent_activity?.at(-1)||null,
    finished:isTerminalStatus(status),
  };
}

export function keywordReducer(state,action){
  switch(action&&action.type){
    case 'run_created':
      return {...initialKeywordState,runId:action.runId||null,status:RUN_STATUS.QUEUED,message:'已提交'};
    case 'run_event':return applyRunEvent(state,action.event);
    case 'snapshot':return applySnapshot(state,action.snapshot);
    default:return state;
  }
}
