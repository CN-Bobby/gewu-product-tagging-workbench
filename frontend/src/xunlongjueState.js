export const activeStatuses=new Set(['queued','pending','running','pausing','resuming','waiting_retry']);

export const normalizeRunStatus=run=>String(run?.status||'unknown').toLowerCase();

const runIdOf=run=>String(run?.id??run?.run_id??'');

export function mergeRunByRevision(current,incoming){
 if(!current||runIdOf(current)!==runIdOf(incoming))return incoming;
 const currentRevision=Number(current.revision??current.metadata?.revision)||0;
 const incomingRevision=Number(incoming.revision??incoming.metadata?.revision)||0;
 const currentUpdated=Date.parse(current.updated_at||'');
 const incomingUpdated=Date.parse(incoming.updated_at||'');
 const stateSource=Number.isFinite(currentUpdated)&&Number.isFinite(incomingUpdated)&&currentUpdated>incomingUpdated?current:incoming;
 const metadataSource=incomingRevision>=currentRevision?incoming:current;
 const metadata=metadataSource===current?{...incoming.metadata,...current.metadata}:{...current.metadata,...incoming.metadata};
 const revision=Math.max(currentRevision,incomingRevision);
 metadata.revision=revision||1;
 return{...stateSource,...metadata,revision:revision||1,metadata};
}

export function mergeRunsByRevision(current,incoming){
 const previous=new Map((current||[]).map(run=>[runIdOf(run),run]));
 return(incoming||[]).map(run=>mergeRunByRevision(previous.get(runIdOf(run)),run));
}

export function progressOf(run){
 const raw=run?.progress??run?.progress_percent??run?.percent_complete;
 if(typeof raw==='number')return Math.max(0,Math.min(100,raw<=1?raw*100:raw));
 if(typeof raw==='object'&&raw){const done=Number(raw.done??raw.completed??0),total=Number(raw.total??raw.expected??0);return total>0?Math.max(0,Math.min(100,done/total*100)):null}
 const done=Number(run?.completed_nodes??run?.nodes_completed??NaN),total=Number(run?.total_nodes??run?.nodes_total??NaN);
 return Number.isFinite(done)&&Number.isFinite(total)&&total>0?Math.max(0,Math.min(100,done/total*100)):null;
}

export function progressLabel(run){
 const raw=run?.progress;
 if(raw&&typeof raw==='object'){
  const done=raw.done??raw.completed,total=raw.total??raw.expected;
  if(done!=null&&total!=null)return`${done} / ${total}${raw.unit?' '+raw.unit:''}`;
  if(raw.message)return String(raw.message);
 }
 const percent=progressOf(run);return percent==null?'进度暂不可用':`${Math.round(percent)}%`;
}

export function retryInfoOf(run){
 const value=run?.retry_info;
 return value&&typeof value==='object'?value:null;
}

export function suspendedCountOf(run){
 const retry=retryInfoOf(run);
 const reported=Number(retry?.suspended_count??run?.suspended_count??run?.suspended_branch_count??run?.node_counts?.SUSPENDED??retry?.exhausted_count);
 if(Number.isFinite(reported)&&reported>0)return Math.floor(reported);
 if(Array.isArray(run?.suspended_branches))return run.suspended_branches.length;
 if(Array.isArray(run?.branches))return run.branches.filter(branch=>String(branch?.status||'').toLowerCase()==='suspended').length;
 return 0;
}

export function runPresentation(run){
 const status=normalizeRunStatus(run);
 const activityStatus=String(run?.activity_status||'').toLowerCase();
 const displayStatus=status==='running'&&activityStatus==='waiting_retry'?activityStatus:status;
 const retryInfo=retryInfoOf(run);
 const suspendedCount=suspendedCountOf(run);
 const hasSuspendedBranches=status==='partial_done'&&suspendedCount>0;
 const rawProgress=progressOf(run);
 const progress=hasSuspendedBranches&&rawProgress!=null?Math.min(rawProgress,99):rawProgress;
 let label=progressLabel(run);
 if(displayStatus==='waiting_retry')label=`等待重试 · ${label}`;
 if(hasSuspendedBranches)label=`阶段完成 · ${suspendedCount} 个分支已暂停`;
 return{status,displayStatus,progress,label,retryInfo,suspendedCount,hasSuspendedBranches};
}
