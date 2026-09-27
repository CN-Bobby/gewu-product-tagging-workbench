import test from 'node:test';
import assert from 'node:assert/strict';
import{activeStatuses,runPresentation}from'../src/xunlongjueState.js';

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
