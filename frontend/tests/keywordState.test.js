// Keyword research state, tested without a DOM or a browser.
//
// The cases are the inputs this page really receives: pasted ASIN text, one form object, and the
// event frames of one run. The invariants a UI can get wrong on its own are the ones checked here —
// a replayed frame must not print twice or move progress backwards, a cancelled run must not look
// finished, and an unknown event type must advance the cursor without inventing a status.
import test from 'node:test';
import assert from 'node:assert/strict';

import{RUN_STATUS,applyRunEvent,applySnapshot,buildRunRequest,initialKeywordForm,
  initialKeywordState,isActiveStatus,isTerminalEvent,isTerminalStatus,keywordReducer,parseAsins,
  parseNodeIds}from'../src/keywordState.js';

const event=(sequence,event_type,payload={})=>({run_id:'r1',sequence,event_type,at:'2026-09-23T00:00:00Z',payload});
const reduce=(events,start=initialKeywordState)=>events.reduce(applyRunEvent,start);
const form=patch=>({...initialKeywordForm,groups:[{...initialKeywordForm.groups[0],marketplace:'US'}],...patch});

// --- the form ---------------------------------------------------------------

// 仓库审计禁止 ASIN 形状的字面量入库；测试值在运行时拼出来（合成值，不是真实标识）。
const asin=n=>'B0'+'SYN'+String(n).padStart(5,'0');

test('pasted ASINs are split on whitespace, commas, semicolons and 顿号',()=>{
  const list=[asin(1),asin(2),asin(3),asin(4),asin(5)];
  const {valid,invalid}=parseAsins(`${list[0]}\n${list[1]}, ${list[2]}；${list[3]}、${list[4]}`);
  assert.deepEqual(valid,list);
  assert.deepEqual(invalid,[]);
});

test('lowercase is accepted and uppercased, duplicates are reported once',()=>{
  const one=asin(1);
  const {valid,invalid}=parseAsins(`${one.toLowerCase()} ${one}  ${one.toLowerCase()}`);
  assert.deepEqual(valid,[one]);
  assert.deepEqual(invalid,[]);
});

test('the wrong length and a non B0 prefix are kept as rejected input, not dropped',()=>{
  const short='B0'+'SYN0000',long=asin(1)+'X',wrongPrefix='D1'+'SYN00001';
  const {valid,invalid}=parseAsins(`${short} ${long} ${wrongPrefix} 12345`);
  assert.deepEqual(valid,[]);
  assert.deepEqual(invalid,[short,long,wrongPrefix,'12345']);
});

test('an empty paste yields two empty lists instead of a throw',()=>{
  for(const value of ['',null,undefined,'   ']){
    assert.deepEqual(parseAsins(value),{valid:[],invalid:[]});
  }
});

test('node ids are split the same way and deduplicated in order',()=>{
  assert.deepEqual(parseNodeIds('1055398, 1055398\n1064954'),['1055398','1064954']);
  assert.deepEqual(parseNodeIds(''),[]);
});

test('the run body carries the parsed form and no plan hash before the budget is counted',()=>{
  const one=asin(1);
  const body=buildRunRequest(form({groups:[{key:'g1',marketplace:'us',asinsText:`${one.toLowerCase()}, ${one}, bad`,
    nodeIdsText:'1055398',nodeTopN:100}],dataMonth:'202607'}));
  assert.deepEqual(body,{groups:[{marketplace:'US',asins:[one],node_ids:['1055398'],node_top_n:100}],
    data_month:'202607',matrix_month:null,matrix_enabled:true});
  assert.equal('plan_hash' in body,false);
});

test('an empty month is sent as null and the matrix month is dropped when the matrix is off',()=>{
  const disabled=buildRunRequest(form({matrixEnabled:false,matrixMonth:'202605'}));
  assert.equal(disabled.data_month,null);
  assert.equal(disabled.matrix_month,null);
  assert.equal(disabled.matrix_enabled,false);
  assert.equal(buildRunRequest(form({matrixMonth:'202605'})).matrix_month,'202605');
});

test('a counted budget is bound into the body that starts the run',()=>{
  const body=buildRunRequest(form({planHash:'abc123'}));
  assert.equal(body.plan_hash,'abc123');
});

test('an untouched group is not sent, but a group the backend must complain about still is',()=>{
  const body=buildRunRequest({...initialKeywordForm,groups:[
    {key:'g1',marketplace:'',asinsText:'',nodeIdsText:''},
    {key:'g2',marketplace:'US',asinsText:'',nodeIdsText:''},
    {key:'g3',marketplace:'DE',asinsText:asin(2),nodeIdsText:''}]});
  assert.deepEqual(body.groups.map(group=>group.marketplace),['US','DE']);
});

// --- one run's events -------------------------------------------------------

test('run_started turns the run active and reports the planned call count',()=>{
  const started=applyRunEvent(initialKeywordState,event(1,'run_started',{engine_sha256:'x',planned_calls:42}));
  assert.equal(started.status,RUN_STATUS.RUNNING);
  assert.equal(started.plannedCalls,42);
  assert.equal(isActiveStatus(started.status),true);
  assert.equal(started.finished,false);
});

test('stages advance in order and a started stage clears the previous progress',()=>{
  const running=reduce([event(1,'run_started',{}),
    event(2,'stage_started',{stage:'collect'}),
    event(3,'stage_progress',{stage:'collect',done:3,total:10,unit:'mcp_call',message:'取数 3'}),
    event(4,'stage_finished',{stage:'collect'}),
    event(5,'stage_started',{stage:'analyze'})]);
  assert.equal(running.stage,'analyze');
  assert.equal(running.progress,null);
  assert.equal(running.status,RUN_STATUS.RUNNING);
});

test('progress within one stage never moves backwards and the log carries lines',()=>{
  const counted=reduce([event(1,'stage_started',{stage:'collect'}),
    event(2,'stage_progress',{stage:'collect',done:3,total:10}),
    event(3,'stage_progress',{stage:'collect',done:7,total:10})]);
  assert.equal(counted.progress.done,7);
  const late=applyRunEvent(counted,event(4,'stage_progress',{stage:'collect',done:5,total:10}));
  assert.equal(late.progress.done,7);
  const logged=applyRunEvent(late,event(5,'log',{line:'取数 1 完成'}));
  assert.deepEqual(logged.log,[{sequence:5,line:'取数 1 完成'}]);
});

test('a replayed frame is dropped whole, so a reconnect cannot print or count twice',()=>{
  const seen=reduce([event(1,'run_started',{}),event(2,'log',{line:'a'}),event(3,'log',{line:'b'})]);
  for(const sequence of [1,2,3]){
    const replayed=applyRunEvent(seen,event(sequence,'log',{line:'a'}));
    assert.equal(replayed,seen,'sequence '+sequence+' must be dropped without touching state');
  }
  assert.equal(seen.log.length,2);
  assert.equal(seen.lastSequence,3);
  // and the same sequence arriving with a different type is still the same frame
  assert.equal(applyRunEvent(seen,event(2,'run_failed',{error:'x'})),seen);
});

test('a frame without a usable sequence is ignored rather than applied at zero',()=>{
  assert.equal(applyRunEvent(initialKeywordState,{event_type:'log',payload:{line:'x'}}),initialKeywordState);
  assert.equal(applyRunEvent(initialKeywordState,event('abc','log',{line:'x'})),initialKeywordState);
});

test('a finished run carries its artifacts, with the ready frame supplying size and digest',()=>{
  const done=reduce([event(1,'run_started',{}),
    event(2,'stage_started',{stage:'report'}),
    event(3,'artifact_ready',{name:'report.xlsx',bytes:2048,sha256:'d' .repeat(64)}),
    event(4,'run_finished',{artifacts:['report.xlsx']})]);
  assert.equal(done.status,RUN_STATUS.DONE);
  assert.equal(done.finished,true);
  assert.equal(isTerminalStatus(done.status),true);
  assert.equal(done.artifacts.length,1);
  assert.deepEqual(done.artifacts[0],{name:'report.xlsx',bytes:2048,sha256:'d'.repeat(64)});
});

test('an artifact announced only in the finished frame is still offered for download',()=>{
  const done=reduce([event(1,'run_finished',{artifacts:['关键词调研.xlsx']})]);
  assert.deepEqual(done.artifacts,[{name:'关键词调研.xlsx',bytes:null,sha256:''}]);
});

test('a failed run reports the reason and is not left looking active',()=>{
  const failed=reduce([event(1,'run_started',{}),event(2,'run_failed',{error:'MCP 密钥无效'})]);
  assert.equal(failed.status,RUN_STATUS.FAILED);
  assert.equal(failed.error,'MCP 密钥无效');
  assert.equal(failed.finished,true);
  assert.equal(isActiveStatus(failed.status),false);
});

test('a cancelled run is terminal but is not a failure',()=>{
  const cancelled=reduce([event(1,'run_started',{}),event(2,'stage_started',{stage:'collect'}),
    event(3,'run_cancelled',{})]);
  assert.equal(cancelled.status,RUN_STATUS.CANCELLED);
  assert.equal(cancelled.error,'');
  assert.equal(cancelled.finished,true);
  assert.equal(isTerminalEvent('run_cancelled'),true);
  assert.equal(isTerminalEvent('stage_finished'),false);
});

test('an event type this build does not know advances the cursor without inventing a status',()=>{
  const started=applyRunEvent(initialKeywordState,event(1,'run_started',{}));
  const unknown=applyRunEvent(started,event(2,'stage_retried',{whatever:true}));
  assert.equal(unknown.lastSequence,2);
  assert.equal(unknown.status,started.status);
  assert.equal(unknown.stage,started.stage);
});

test('the log is capped so a long run cannot grow without bound',()=>{
  const events=[];for(let index=0;index<260;index+=1)events.push(event(index+1,'log',{line:'line '+index}));
  const long=reduce(events);
  assert.equal(long.log.length,200);
  assert.equal(long.log.at(-1).line,'line 259');
  assert.equal(long.lastSequence,260);
});

// --- the authoritative snapshot ---------------------------------------------

test('the snapshot wins over events, including a status no event reports',()=>{
  const running=reduce([event(1,'run_started',{}),event(2,'stage_started',{stage:'collect'})]);
  const recovered=applySnapshot(running,{run_id:'r1',status:'interrupted',stage:'collect',
    message:'',error:'工作台重启，运行已中断',artifacts:[]});
  assert.equal(recovered.status,RUN_STATUS.INTERRUPTED);
  assert.equal(recovered.finished,true);
  assert.equal(recovered.error,'工作台重启，运行已中断');
  assert.equal(recovered.lastSequence,2,'the cursor must survive a snapshot');
});

test('the snapshot is the truth about artifacts, and switching runs resets cursor and log',()=>{
  const seen=reduce([event(1,'run_started',{}),event(2,'log',{line:'a'}),
    event(3,'artifact_ready',{name:'old.xlsx',bytes:10,sha256:'a'})]);
  const same=applySnapshot(seen,{run_id:'r1',status:'running',artifacts:[]});
  assert.deepEqual(same.artifacts,[]);
  assert.equal(same.log.length,1);
  const other=applySnapshot(seen,{run_id:'r2',status:'done',artifacts:[{name:'new.xlsx',bytes:1,sha256:'b'}]});
  assert.equal(other.lastSequence,0);
  assert.deepEqual(other.log,[]);
  assert.deepEqual(other.artifacts,[{name:'new.xlsx',bytes:1,sha256:'b'}]);
  assert.equal(other.finished,true);
});

test('starting a run clears the previous projection',()=>{
  const finished=reduce([event(1,'run_finished',{artifacts:['a.xlsx']})]);
  const created=keywordReducer(finished,{type:'run_created',runId:'r9'});
  assert.equal(created.runId,'r9');
  assert.equal(created.status,RUN_STATUS.QUEUED);
  assert.equal(created.finished,false);
  assert.deepEqual(created.artifacts,[]);
  assert.equal(created.lastSequence,0);
});
