// Workbench proposal/task projection, tested without a DOM or a browser.
//
// A proposal and its task are dataset state: the panel must show them from that state alone, even
// when no assistant reply mentions them and even when the proposal belongs to no conversation. Every
// case here asserts one of the rules that makes that true — dataset scoping, "pending only" for the
// approval area, the four proposal facts, verbatim task statuses, and structural (not textual)
// detection of a failed launch.
import test from 'node:test';
import assert from 'node:assert/strict';

import {PROPOSAL_STATUS,PROPOSAL_VIEW,TASK_VIEW,ACTIVE_TASK_STATUSES,LAUNCH_ERROR_KEY,
  changeView,columnName,inDataset,proposalView,selectPending,selectProposals,selectTasks,taskView,workbenchActions}
  from '../src/proposalState.js';

const columns=[{id:'c1',key:'color',revision:2,definition:{name:'色系列'}}];

const proposal=(over={})=>({id:'p1',dataset_id:'d1',title:'色系列：收敛为12类单值，全量重跑',
  status:PROPOSAL_STATUS.PENDING,group_revision:1,created_at:'2026-09-17T10:00:00+00:00',
  changes:[],...over});

// What an agent-produced proposal really looks like: no turn identity, no conversation, because
// nothing writes the legacy active-turn marker any more.
const agentProposal=(over={})=>proposal({origin_turn_id:null,changes:[executeChange()],...over});

const executeChange=(over={})=>({kind:'execute',column_ids:['c1'],product_ids:['a','b','c'],
  revisions:{c1:2},overwrite:false,refresh_images:false,...over});

function revisionChange({mode='invalidate_all',impact={total:310,preserved:0,invalidated:310},summary=true}={}){
  return {kind:'revise',column_id:'c1',expected_revision:2,definition:{key:'color',name:'色系列',definition:'收敛为12类单值'},
    impact_mode:mode,impact,
    repair_summary:summary?[{column_id:'c1',name:'色系列',failed:0,rerun:310,preserved_success:0,protected:17,
      estimated_requests:59,reason:'定义语义或执行契约改变，后端按整列计算实际影响范围'}]:undefined};
}

const job=(over={})=>({id:'j1',dataset_id:'d1',proposal_id:'p1',status:'running',
  created_at:'2026-09-17T10:01:00+00:00',
  spec:{kind:'labels',sample:false,product_count:310,
    columns:[{id:'c1',revision:3,definition:{name:'色系列'}}]},
  completed_count:0,expected_count:310,error_count:0,error_summary:[],
  metrics:{requests:4,vision_requests:2,cache_hits:1,wall_seconds:8.4},...over});

// --- selection -------------------------------------------------------------

test('an agent proposal reaches the panel from dataset state alone',()=>{
  const {pending}=workbenchActions([agentProposal()],[],columns,'d1');
  assert.equal(pending.length,1);
  assert.equal(pending[0].actionable,true);
  assert.equal(pending[0].view,PROPOSAL_VIEW.AWAITING_APPROVAL);
});

test('pending proposals are listed newest first',()=>{
  const older=proposal({id:'p-old',created_at:'2026-09-17T09:00:00+00:00'});
  const newer=proposal({id:'p-new',created_at:'2026-09-17T11:00:00+00:00'});
  assert.deepEqual(selectPending([older,newer],'d1').map(p=>p.id),['p-new','p-old']);
});

test('superseded, rejected and confirmed proposals offer no approval',()=>{
  const proposals=[proposal({id:'p-sup',status:PROPOSAL_STATUS.SUPERSEDED}),
    proposal({id:'p-rej',status:PROPOSAL_STATUS.REJECTED}),
    agentProposal({id:'p-ok',status:PROPOSAL_STATUS.CONFIRMED,changes:[executeChange()]})];
  assert.deepEqual(selectPending(proposals,'d1'),[]);
  const views=proposals.map(p=>proposalView(p,columns).view);
  assert.deepEqual(views,[PROPOSAL_VIEW.SUPERSEDED,PROPOSAL_VIEW.REJECTED,PROPOSAL_VIEW.APPROVED]);
  assert.deepEqual(proposals.map(p=>proposalView(p,columns).actionable),[false,false,false]);
});

test('another dataset never contributes proposals or tasks',()=>{
  const other=proposal({id:'p-other',dataset_id:'d2'});
  const otherJob=job({id:'j-other',dataset_id:'d2'});
  const {pending,tasks}=workbenchActions([proposal(),other],[job(),otherJob],columns,'d1');
  assert.deepEqual(pending.map(p=>p.id),['p1']);
  assert.deepEqual(tasks.map(j=>j.id),['j1']);
  assert.equal(inDataset(other,'d1'),false);
});

// --- proposal view ---------------------------------------------------------

test('a confirmed proposal without an execute change is complete on its own',()=>{
  const view=proposalView(proposal({status:PROPOSAL_STATUS.CONFIRMED,
    changes:[{kind:'add',definition:{key:'color',name:'色系列'}}]}),columns);
  assert.equal(view.view,PROPOSAL_VIEW.APPROVED_NO_TASK);
  assert.equal(view.hasExecution,false);
  assert.equal(view.canSample,false);
  assert.equal(view.confirmLabel,'确认变更');
  assert.equal(view.totals.products,0);
});

test('a revision view reports the revision it would commit, in cells',()=>{
  const view=proposalView(proposal({changes:[revisionChange(),executeChange()]}),columns);
  const revised=view.changes[0];
  assert.equal(revised.impact.currentRevision,2);
  assert.equal(revised.impact.proposedRevision,3);
  assert.equal(revised.impact.mode,'invalidate_all');
  assert.equal(view.totals.invalidatedCells,310);
  assert.equal(view.totals.preservedCells,0);
  assert.equal(view.totals.columns,1);
  assert.equal(view.totals.products,3);
  assert.equal(view.totals.estimatedRequests,59);
});

test('a compatible revision reports preserved cells instead of invalidated ones',()=>{
  const view=proposalView(proposal({changes:[revisionChange(
    {mode:'preserve_compatible',impact:{total:310,preserved:293,invalidated:0}})]}),columns);
  assert.equal(view.totals.preservedCells,293);
  assert.equal(view.totals.invalidatedCells,0);
});

test('an estimate the backend did not compute stays unknown',()=>{
  const view=proposalView(proposal({changes:[revisionChange({summary:false})]}),columns);
  assert.equal(view.totals.estimatedRequests,null);
  assert.deepEqual(view.changes[0].estimates,[]);
});

test('a new column executed in the same turn is named from the plan',()=>{
  const plan=[{kind:'add',planned_id:'planned-1',definition:{key:'color',name:'色系列'}},
    executeChange({column_ids:['planned-1']})];
  assert.equal(columnName('planned-1',columns,plan),'色系列');
  assert.equal(columnName('c1',columns,plan),'色系列');
  assert.equal(columnName('gone',columns,plan),'gone');
  assert.deepEqual(changeView(plan[1],columns,plan).columns,['色系列']);
});

// --- task view -------------------------------------------------------------

test('the task area holds unfinished work and nothing else',()=>{
  const jobs=[job({id:'j-run',status:'running'}),job({id:'j-done',status:'completed'}),
    job({id:'j-cancel',status:'cancelled'}),job({id:'j-partial',status:'partial'}),
    job({id:'j-interrupted',status:'interrupted'}),job({id:'j-archived',status:'running',archived:true})];
  assert.deepEqual(selectTasks(jobs,'d1').map(j=>j.id),['j-run','j-partial','j-interrupted']);
  assert.deepEqual(ACTIVE_TASK_STATUSES.includes('completed'),false);
});

test('a task card reports cell progress, scope and the buttons the worksheet offers',()=>{
  const view=taskView(job(),[agentProposal()]);
  assert.equal(view.title,'色系列：收敛为12类单值，全量重跑');
  assert.deepEqual(view.columns,['色系列']);
  assert.equal(view.progress.total,310);
  assert.equal(view.progress.done,0);
  assert.equal(view.progress.products,310);
  assert.equal(view.view,TASK_VIEW.RUNNING);
  assert.equal(view.label,'执行中');
  assert.deepEqual(view.actions,{pause:true,cancel:true,resume:false,detail:true});
  assert.equal(view.metrics.seconds,8.4);
  assert.deepEqual(taskView(job({status:'interrupted'}),[]).actions,{pause:false,cancel:false,resume:true,detail:true});
  assert.deepEqual(taskView(job({status:'paused'}),[]).actions,{pause:false,cancel:false,resume:true,detail:true});
});

test('a status the backend does not have is shown raw, not given a made-up view',()=>{
  const view=taskView(job({status:'teleporting'}),[]);
  assert.equal(view.view,null);
  assert.equal(view.label,'teleporting');
  assert.equal(view.launchFailed,false);
  assert.deepEqual(view.actions,{pause:false,cancel:false,resume:false,detail:true});
});

test('a failed launch is read from the error key, not from the message text',()=>{
  const failed=taskView(job({status:'interrupted',error_count:1,error_keys:[LAUNCH_ERROR_KEY],
    error_summary:['任务未启动（launcher 未接受该任务）']}),[]);
  assert.equal(failed.launchFailed,true);
  assert.equal(failed.needsAttention,true);
  const plain=taskView(job({status:'interrupted',error_count:1,
    error_summary:['任务未启动（launcher 未接受该任务）']}),[]);
  assert.equal(plain.launchFailed,false,'a summary that happens to mention launching proves nothing');
});
