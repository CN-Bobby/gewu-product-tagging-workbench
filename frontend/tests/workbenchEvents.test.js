// Which draft-stream events the workbench still applies, in the default (agent) panel mode.
//
// The draft stream is shared: the legacy chat's messages and questions travel on the same stream as
// the worksheet, draft, batch and proposal state. Cutting the default panel over to the agent must
// not disturb the business half of that stream, so the classification and the projection are
// asserted here without a browser.
import test from 'node:test';
import assert from 'node:assert/strict';

import {applyDraftEvent,draftEventOwner,isLegacyChatKind,mergeById,LEGACY_CHAT_KINDS}
  from '../src/workbenchEvents.js';

const worksheet=()=>({
  columns:[{id:'c1',definition:{name:'颜色'}}],
  rows:[{id:'p1',anchor:'A-1',cells:{c1:{status:'pending',value:null}},text_record:null},
        {id:'p2',anchor:'A-2',cells:{c1:{status:'ready',value:'红'}},text_record:null}],
  messages:[{id:'m1',text:'旧',presentation:null,error_code:null}],
  questions:[],proposals:[],jobs:[],versions:[],
});

// --- the business half keeps working --------------------------------------

test('a cell event still updates the worksheet',()=>{
  const next=applyDraftEvent(worksheet(),'cell',{product_id:'p1',column_id:'c1',status:'ready',value:'蓝'});
  assert.equal(next.rows[0].cells.c1.value,'蓝');
  assert.equal(next.rows[0].cells.c1.status,'ready');
  assert.equal(next.rows[1].cells.c1.value,'红');   // the other row is untouched
});

test('a text_record event still updates the worksheet',()=>{
  const next=applyDraftEvent(worksheet(),'text_record',{product_id:'p2',status:'ready',content:'文字底稿'});
  assert.equal(next.rows[1].text_record.content,'文字底稿');
  assert.equal(next.rows[0].text_record,null);
});

test('collection events still grow the worksheet collections',()=>{
  for(const [kind,collection,start] of [['proposal','proposals',0],['job','jobs',0],
                                        ['version','versions',0],['column','columns',1]]){
    const next=applyDraftEvent(worksheet(),kind,{id:kind+'-1',name:kind});
    assert.equal(next[collection].length,start+1,kind);
    assert.equal(next[collection].at(-1).id,kind+'-1');
  }
});

test('a repeated collection event merges instead of duplicating',()=>{
  const first=applyDraftEvent(worksheet(),'job',{id:'j1',status:'queued'});
  const second=applyDraftEvent(first,'job',{id:'j1',status:'running'});
  assert.equal(second.jobs.length,1);
  assert.equal(second.jobs[0].status,'running');
});

test('an unknown kind changes nothing and invents nothing',()=>{
  const data=worksheet();
  assert.equal(applyDraftEvent(data,'weird_future_event',{id:'x'}),data);
  assert.equal(applyDraftEvent(null,'cell',{product_id:'p1'}),null);
});

// --- the legacy chat half is classified, not dropped ----------------------

test('the legacy chat projections are named as such',()=>{
  for(const kind of ['chat_chunk','message_presentation','message','question'])
    assert.equal(draftEventOwner(kind),'legacy_chat',kind);
  for(const kind of ['cell','text_record','proposal','proposal_group','job','version','column','reload'])
    assert.equal(draftEventOwner(kind),'workbench',kind);
  assert.deepEqual([...LEGACY_CHAT_KINDS].sort(),
    ['chat_chunk','message','message_presentation','question']);
});

test('a legacy chat event is still applied rather than filtered away',()=>{
  const next=applyDraftEvent(worksheet(),'chat_chunk',{id:'m1',text:'回答'});
  assert.equal(next.messages[0].text,'旧回答');
  const presented=applyDraftEvent(worksheet(),'message_presentation',
    {id:'m1',presentation:{heading:'结论'},error_code:null});
  assert.equal(presented.messages[0].presentation.heading,'结论');
});

test('mergeById keeps order and replaces by id',()=>{
  const merged=mergeById([{id:'a',n:1},{id:'b',n:2}],{id:'a',n:9});
  assert.deepEqual(merged,[{id:'a',n:9},{id:'b',n:2}]);
  assert.deepEqual(mergeById([{id:'a'}],{id:'c'}),[{id:'a'},{id:'c'}]);
});
