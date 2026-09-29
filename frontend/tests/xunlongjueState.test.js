import test from 'node:test';
import assert from 'node:assert/strict';
import{activeStatuses,mergeRunByRevision,mergeRunsByRevision,runPresentation}from'../src/xunlongjueState.js';

test('waiting retry remains active and exposes retry context at zero progress',()=>{
 const presentation=runPresentation({status:'waiting_retry',progress:0,retry_info:{waiting_count:2,exhausted_count:0,next_retry_at:'2026-09-27T10:00:00Z',last_error:'temporary timeout',last_node_id:'123',last_attempt:2,max_attempts:4}});
 assert.ok(activeStatuses.has(presentation.status));
 assert.equal(presentation.progress,0);
 assert.equal(presentation.label,'等待重试 · 0%');
 assert.equal(presentation.retryInfo.last_attempt,2);
 assert.equal(presentation.retryInfo.next_retry_at,'2026-09-27T10:00:00Z');
 assert.equal(presentation.retryInfo.last_error,'temporary timeout');
});

test('running tasks display their waiting retry activity without changing backend status',()=>{
 const presentation=runPresentation({status:'running',activity_status:'waiting_retry',progress:0});
 assert.equal(presentation.status,'running');
 assert.equal(presentation.displayStatus,'waiting_retry');
 assert.equal(presentation.progress,0);
 assert.equal(presentation.label,'等待重试 · 0%');
});

test('partial completion with exhausted branches is clearly partial and never reports 100 percent',()=>{
 const presentation=runPresentation({status:'partial_done',progress:100,retry_info:{exhausted_count:3,last_error:'rate limited'}});
 assert.equal(presentation.hasSuspendedBranches,true);
 assert.equal(presentation.suspendedCount,3);
 assert.equal(presentation.progress,99);
 assert.equal(presentation.label,'阶段完成 · 3 个分支已暂停');
});

test('partial label uses suspended node count instead of failed request count',()=>{
 const presentation=runPresentation({status:'partial_done',progress:100,retry_info:{suspended_count:1,exhausted_count:3}});
 assert.equal(presentation.suspendedCount,1);
 assert.equal(presentation.label,'阶段完成 · 1 个分支已暂停');
});

test('suspended branch records trigger partial treatment when aggregate count is absent',()=>{
 const presentation=runPresentation({status:'partial_done',progress:1,suspended_branches:[{node_id:'456'}]});
 assert.equal(presentation.hasSuspendedBranches,true);
 assert.equal(presentation.suspendedCount,1);
 assert.equal(presentation.progress,99);
});

test('ordinary active and completed runs retain normal progress presentation',()=>{
 const active=runPresentation({status:'running',progress:{done:2,total:5}});
 assert.ok(activeStatuses.has(active.status));
 assert.equal(active.progress,40);
 assert.equal(active.label,'2 / 5');
 const completed=runPresentation({status:'completed',progress:100});
 assert.equal(completed.hasSuspendedBranches,false);
 assert.equal(completed.progress,100);
 assert.equal(completed.label,'100%');
});

test('late task detail responses preserve the newest saved name and note',()=>{
 const current={run_id:'run-1',revision:4,updated_at:'2026-09-29T10:02:00Z',metadata:{revision:4,name:'new name',note:'latest note'},status:'running',progress:25};
 const stale={run_id:'run-1',revision:3,updated_at:'2026-09-29T10:01:00Z',metadata:{revision:3,name:'old name',note:'old note'},status:'running',progress:30};
 const merged=mergeRunByRevision(current,stale);
 assert.equal(merged.revision,4);
 assert.equal(merged.name,'new name');
 assert.equal(merged.metadata.note,'latest note');
 assert.equal(merged.progress,25);
});

test('late task list responses keep newer metadata while refreshing task status',()=>{
 const current=[{id:'run-1',revision:5,updated_at:'2026-09-29T10:01:00Z',metadata:{revision:5,name:'renamed',note:'saved'},status:'running'}];
 const incoming=[{id:'run-1',revision:2,updated_at:'2026-09-29T10:02:00Z',metadata:{revision:2,name:'old'},status:'completed'}];
 const merged=mergeRunsByRevision(current,incoming);
 assert.equal(merged[0].status,'completed');
 assert.equal(merged[0].revision,5);
 assert.equal(merged[0].metadata.name,'renamed');
 assert.equal(merged[0].metadata.note,'saved');
});

test('a slower poll cannot undo a newer pause or retry state',()=>{
 const current={run_id:'run-2',revision:1,updated_at:'2026-09-29T10:03:00Z',status:'paused',progress:40,message:'已暂停'};
 const late={run_id:'run-2',revision:1,updated_at:'2026-09-29T10:02:00Z',status:'running',progress:70,message:''};
 const merged=mergeRunByRevision(current,late);
 assert.equal(merged.status,'paused');
 assert.equal(merged.progress,40);
 assert.equal(merged.message,'已暂停');
});
