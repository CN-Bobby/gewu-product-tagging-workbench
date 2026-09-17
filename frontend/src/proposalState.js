// Workbench domain projection for the agent panel.
//
// A proposal and the task it creates are dataset state, not turn state: they outlive the agent
// session that produced them, and the panel reads them from the same two places every other panel
// does (GET /api/drafts/{id} and the draft event stream). Nothing here knows about agent events,
// assistant text or turns — which is exactly what makes a pending proposal visible even when no
// reply mentions it.
//
// Pure module on purpose: no React, no DOM, no fetch, so every rule below is testable directly.

export const PROPOSAL_STATUS={PENDING:'pending',CONFIRMED:'confirmed',REJECTED:'rejected',SUPERSEDED:'superseded'};

// Views describe the four facts a proposal can persist. `cancelled` is a task fact, never a
// proposal one, so it is not invented here.
export const PROPOSAL_VIEW={AWAITING_APPROVAL:'awaiting_approval',APPROVED:'approved',
  APPROVED_NO_TASK:'approved_no_task',REJECTED:'rejected',SUPERSEDED:'superseded'};

export const TASK_VIEW={QUEUED:'queued',RUNNING:'running',PAUSED:'paused',
  CANCELLED:'cancelled',COMPLETED:'completed',PARTIAL:'partial',INTERRUPTED:'interrupted'};

// Verbatim backend statuses only. Two statuses may share one view (pausing reads as paused), but no
// view is invented for a status the backend does not have.
const TASK_VIEW_BY_STATUS={queued:TASK_VIEW.QUEUED,running:TASK_VIEW.RUNNING,pausing:TASK_VIEW.PAUSED,
  paused:TASK_VIEW.PAUSED,cancelling:TASK_VIEW.CANCELLED,cancelled:TASK_VIEW.CANCELLED,
  completed:TASK_VIEW.COMPLETED,partial:TASK_VIEW.PARTIAL,interrupted:TASK_VIEW.INTERRUPTED};

const TASK_LABEL={queued:'已排队',running:'执行中',pausing:'正在暂停',paused:'已暂停',
  cancelling:'正在取消',cancelled:'已取消',completed:'已完成',partial:'部分失败',interrupted:'已中断'};

// Active work is work the user may still need to act on. Finished and cancelled tasks leave this
// area instead of accumulating in it.
export const ACTIVE_TASK_STATUSES=['queued','running','pausing','paused','cancelling','partial','interrupted'];

// The one error key the UI reads structurally. Matching on the summary text instead would be a
// string protocol; a key list survives a refresh because it travels in the job brief.
export const LAUNCH_ERROR_KEY='launch';

const CHANGE_LABEL={add:'新增',revise:'修订',execute:'执行',expand:'整类属性列',
  collapse:'恢复单列',archive_column:'归档列',restore_column:'恢复列',correct:'修正'};

// The same action sets the worksheet's own task list uses, so the two surfaces cannot disagree
// about which button a task offers.
const PAUSABLE=['queued','running'];
const CANCELLABLE=['queued','running','pausing'];
const RESUMABLE=['paused','partial','interrupted'];

const asList=value=>Array.isArray(value)?value:[];

export const inDataset=(record,did)=>record?.dataset_id===did;

// Newest first. Proposals are scoped by dataset id, never by the legacy conversation the old card
// was filtered by: an agent-produced proposal belongs to no conversation, which is why that filter
// hid it.
export function selectProposals(proposals,did){
  return asList(proposals).filter(record=>inDataset(record,did))
    .sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
}

// Everything the user can still approve. Superseded, rejected and already-confirmed proposals are
// history, and a superseded one must never offer an approve button.
export function selectPending(proposals,did){
  return selectProposals(proposals,did).filter(p=>p.status===PROPOSAL_STATUS.PENDING);
}

// One task area: unfinished work plus the two states that need a decision (partial, interrupted).
// Completed and cancelled tasks are not kept here.
export function selectTasks(jobs,did){
  return asList(jobs).filter(j=>inDataset(j,did)&&!j.archived&&ACTIVE_TASK_STATUSES.includes(j.status))
    .sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
}

// A column proposed and executed in one turn has no row yet: execute changes reference it by the
// planned id the proposal itself carries.
export function columnName(id,columns,plan){
  const saved=asList(columns).find(c=>c.id===id);
  if(saved)return saved.definition?.name||id;
  const planned=asList(plan).find(change=>change?.planned_id===id);
  return planned?.definition?.name||id||'未知列';
}

function reviseImpact(change){
  const current=change?.expected_revision;
  const known=Number.isFinite(current);
  return {mode:change?.impact_mode||null,
    currentRevision:known?current:null,proposedRevision:known?current+1:null,
    affected:Number(change?.impact?.total)||0,
    preserved:Number(change?.impact?.preserved)||0,
    invalidated:Number(change?.impact?.invalidated)||0};
}

export function changeView(change,columns,plan){
  const kind=change?.kind||'unknown';
  const productIds=asList(change?.product_ids);
  const columnIds=asList(change?.column_ids);
  return {kind,label:CHANGE_LABEL[kind]||kind,
    columnIds,columns:columnIds.map(id=>columnName(id,columns,plan)),
    column:change?.column_id?columnName(change.column_id,columns,plan):null,
    productIds,products:productIds.length,
    overwrite:change?.overwrite===true,refreshImages:change?.refresh_images===true,
    attributes:asList(change?.attributes),
    definition:change?.definition||null,
    value:change?.value===undefined?null:change.value,
    // Computed by the backend only for a revision that invalidates results, so a missing estimate
    // stays null instead of becoming a made-up zero.
    estimates:asList(change?.repair_summary).map(item=>({
      column:item?.name||null,failed:Number(item?.failed)||0,rerun:Number(item?.rerun)||0,
      preserved:Number(item?.preserved_success)||0,protected:Number(item?.protected)||0,
      estimatedRequests:Number.isFinite(item?.estimated_requests)?item.estimated_requests:null,
      reason:item?.reason||null})),
    impact:kind==='revise'?reviseImpact(change):null};
}

export function proposalView(proposal,columns){
  const plan=asList(proposal?.changes);
  const changes=plan.map(change=>changeView(change,columns,plan));
  const executions=changes.filter(view=>view.kind==='execute');
  const hasExecution=executions.length>0;
  const status=proposal?.status;
  const view=status===PROPOSAL_STATUS.PENDING?PROPOSAL_VIEW.AWAITING_APPROVAL
    :status===PROPOSAL_STATUS.CONFIRMED?(hasExecution?PROPOSAL_VIEW.APPROVED:PROPOSAL_VIEW.APPROVED_NO_TASK)
    :status===PROPOSAL_STATUS.REJECTED?PROPOSAL_VIEW.REJECTED
    :status===PROPOSAL_STATUS.SUPERSEDED?PROPOSAL_VIEW.SUPERSEDED:null;
  const invalidatedCells=changes.reduce((sum,v)=>sum+(v.impact?.mode==='invalidate_all'?v.impact.invalidated:0),0);
  const preservedCells=changes.reduce((sum,v)=>sum+(v.impact?.mode==='preserve_compatible'?v.impact.preserved:0),0);
  const estimates=changes.flatMap(v=>v.estimates.map(item=>item.estimatedRequests)).filter(x=>x!==null);
  return {id:proposal?.id,title:proposal?.title||'变更提案',status,view,
    revision:Number(proposal?.group_revision)||1,actionable:view===PROPOSAL_VIEW.AWAITING_APPROVAL,
    createdAt:proposal?.created_at||null,confirmedAt:proposal?.confirmed_at||null,
    changes,hasExecution,
    // Confirming a proposal with no execute change completes it: there is nothing to run, and the
    // panel must not wait for a task that will never exist.
    confirmLabel:hasExecution?'确认并执行':'确认变更',canSample:hasExecution,
    totals:{columns:changes.filter(v=>['add','revise'].includes(v.kind)).length,
      products:new Set(executions.flatMap(v=>v.productIds)).size,
      invalidatedCells,preservedCells,
      estimatedRequests:estimates.length?estimates.reduce((a,b)=>a+b,0):null}};
}

export function taskView(job,proposals){
  const owner=asList(proposals).find(p=>p.id===job?.proposal_id);
  const status=job?.status||'';
  const metrics=job?.metrics||{};
  return {id:job?.id,proposalId:job?.proposal_id||null,
    title:owner?.title||(job?.spec?.kind==='text_facts'?'文字底稿拆解':'执行任务'),
    status,view:TASK_VIEW_BY_STATUS[status]||null,label:TASK_LABEL[status]||status,
    columns:asList(job?.spec?.columns).map(c=>c.definition?.name||c.id),
    // Progress is counted in cells because that is the one number the backend computes. The product
    // count is the task's scope, not a second progress bar.
    progress:{done:Number(job?.completed_count)||0,total:Number(job?.expected_count)||0,
      products:Number(job?.spec?.product_count)||0,failed:Number(job?.error_count)||0},
    sample:job?.spec?.sample===true,
    launchFailed:asList(job?.error_keys).includes(LAUNCH_ERROR_KEY),
    needsAttention:status==='partial'||status==='interrupted',
    errorSummary:asList(job?.error_summary),
    metrics:{requests:Number(metrics.requests)||0,vision:Number(metrics.vision_requests)||0,
      cache:Number(metrics.cache_hits)||0,
      seconds:Number.isFinite(metrics.wall_seconds)?metrics.wall_seconds:null},
    actions:{pause:PAUSABLE.includes(status),cancel:CANCELLABLE.includes(status),
      resume:RESUMABLE.includes(status),detail:true},
    createdAt:job?.created_at||null};
}

// The single entry the panel uses: what waits for the user, and what is running.
export function workbenchActions(proposals,jobs,columns,did){
  return {pending:selectPending(proposals,did).map(p=>proposalView(p,columns)),
          tasks:selectTasks(jobs,did).map(j=>taskView(j,proposals))};
}
