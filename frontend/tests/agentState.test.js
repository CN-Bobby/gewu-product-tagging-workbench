// Agent panel state machine, tested without a DOM or a browser.
//
// Every case here is a sequence of the two inputs the panel really receives: a command result and
// an application event. The point is the invariants a UI can get wrong on its own — a cancelled
// turn must not lock the panel, a failed turn must not end the session, a replayed event must not
// duplicate a message, and a session without a backend must not look usable.
import test from 'node:test';
import assert from 'node:assert/strict';

import {agentReducer,initialAgentState,canSend,pendingInteractions,SESSION_STATUS,TURN_STATUS}
  from '../src/agentState.js';

function reduce(actions,start=initialAgentState){
  return actions.reduce(agentReducer,start);
}

const event=(sequence,event_type,payload={})=>({type:'agent_event',event:{sequence,event_type,payload}});
const openSession=()=>reduce([{type:'session_creating'},{type:'session_created',sessionId:'s1'},
  {type:'session_started',sessionId:'s1',remoteSessionId:'remote-1'}]);

// --- start -----------------------------------------------------------------

test('create then start leaves the session active and the composer enabled',()=>{
  const created=reduce([{type:'session_creating'},{type:'session_created',sessionId:'s1'}]);
  assert.equal(created.sessionStatus,SESSION_STATUS.STARTING);
  const ready=agentReducer(created,{type:'session_started',sessionId:'s1',remoteSessionId:'remote-1'});
  assert.equal(ready.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(ready.remoteSessionId,'remote-1');
  assert.equal(canSend(ready),true);
});

test('a session that exists without a backend is unavailable, not active',()=>{
  const stale=agentReducer(openSession(),{type:'session_snapshot',
    snapshot:{session_id:'s1',status:'active',remote_session_id:'remote-1',backend:null}});
  assert.equal(stale.sessionStatus,SESSION_STATUS.UNAVAILABLE);
  assert.equal(stale.backendAvailable,false);
  assert.equal(canSend(stale),false);
  const live=agentReducer(openSession(),{type:'session_snapshot',
    snapshot:{session_id:'s1',status:'active',remote_session_id:'remote-1',backend:{alive:true}}});
  assert.equal(live.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(canSend(live),true);
});

// --- a normal turn ---------------------------------------------------------

test('a normal turn aggregates the reply and frees the composer',()=>{
  const sent=agentReducer(openSession(),{type:'turn_sending',text:'你好'});
  assert.equal(sent.turnStatus,TURN_STATUS.SENDING);
  assert.equal(canSend(sent),false);
  const streamed=reduce([event(5,'message_started',{message_id:'m1'}),
    event(6,'assistant_text_delta',{text:'你',message_id:'m1'}),
    event(7,'assistant_text_delta',{text:'好',message_id:'m1'})],sent);
  assert.deepEqual(streamed.items.filter(item=>item.kind==='assistant').map(item=>item.text),['你好']);
  assert.equal(streamed.items.filter(item=>item.kind==='assistant').length,1);   // one bubble, not two
  const settled=reduce([event(8,'turn_settled',{stop_reason:'end_turn'}),event(9,'message_completed',{})],streamed);
  assert.equal(settled.turnStatus,TURN_STATUS.IDLE);
  assert.equal(settled.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(canSend(settled),true);
  assert.equal(settled.items.at(-1).status,'done');
});

test('deltas without a message id still aggregate into one reply',()=>{
  const streamed=reduce([{type:'turn_sending',text:'x'},event(1,'assistant_text_delta',{text:'a'}),
    event(2,'assistant_text_delta',{text:'b'})],openSession());
  assert.deepEqual(streamed.items.filter(item=>item.kind==='assistant').map(item=>item.text),['ab']);
});

// --- cancel ----------------------------------------------------------------

test('cancel asks, then the cancelled turn frees the composer with the session alive',()=>{
  const asked=reduce([{type:'turn_sending',text:'慢慢说'},{type:'cancel_requested',accepted:true}],openSession());
  assert.equal(asked.turnStatus,TURN_STATUS.CANCELLING);
  assert.equal(canSend(asked),false);                       // asked for, not finished
  const cancelled=reduce([event(3,'assistant_text_delta',{text:'半',message_id:'m1'}),
    event(4,'message_cancelled',{message_id:'m1'})],asked);
  assert.equal(cancelled.turnStatus,TURN_STATUS.IDLE);
  assert.equal(cancelled.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(canSend(cancelled),true);
  assert.equal(cancelled.items.find(item=>item.kind==='assistant').status,'cancelled');
});

test('a refused cancel request does not move the turn',()=>{
  const idle=reduce([{type:'cancel_requested',accepted:false}],openSession());
  assert.equal(idle.turnStatus,TURN_STATUS.IDLE);
  assert.equal(canSend(idle),true);
});

test('a turn cancelled by the user is not a stopped session',()=>{
  const state=reduce([{type:'turn_sending',text:'x'},{type:'cancel_requested',accepted:true},
    event(1,'turn_settled',{stop_reason:'cancelled'}),event(2,'message_cancelled',{})],openSession());
  assert.equal(state.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.notEqual(state.sessionStatus,SESSION_STATUS.STOPPED);
});

// --- failures --------------------------------------------------------------

test('a failed turn reports an error and the session stays usable',()=>{
  const failed=reduce([{type:'turn_sending',text:'坏的'},event(1,'message_started',{message_id:'m1'}),
    event(2,'message_failed',{message_id:'m1',error:'TurnError',fatal:false})],openSession());
  assert.equal(failed.turnStatus,TURN_STATUS.IDLE);
  assert.equal(failed.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(failed.error.fatal,false);
  assert.equal(canSend(failed),true);
});

test('an application failure that is not fatal leaves the panel usable',()=>{
  const state=reduce([{type:'turn_sending',text:'x'},
    {type:'command_failed',scope:'turn',error:{code:'turn_error',message:'回合执行失败',fatal:false}}],openSession());
  assert.equal(state.sessionStatus,SESSION_STATUS.ACTIVE);
  assert.equal(state.error.code,'turn_error');
  assert.equal(canSend(state),true);
});

test('a fatal failure locks the panel and marks the session failed',()=>{
  const state=reduce([{type:'turn_sending',text:'x'},
    {type:'command_failed',scope:'turn',error:{code:'runtime_unavailable',message:'运行时不可用',fatal:true}}],openSession());
  assert.equal(state.sessionStatus,SESSION_STATUS.FAILED);
  assert.equal(state.error.fatal,true);
  assert.equal(canSend(state),false);
});

test('session_failed locks the panel even without a command error',()=>{
  const state=reduce([event(1,'session_failed',{})],openSession());
  assert.equal(state.sessionStatus,SESSION_STATUS.FAILED);
  assert.equal(canSend(state),false);
});

// --- tools -----------------------------------------------------------------

test('a tool lifecycle shows up in order and settles once',()=>{
  const state=reduce([{type:'turn_sending',text:'看下表'},
    event(1,'tool_call_started',{tool_call_id:'c1',server:'m3b7_fixture',tool:'echo_tool',host_status:'pending'}),
    event(2,'tool_call_settled',{tool_call_id:'c1',server:'m3b7_fixture',tool:'echo_tool',host_status:'completed'}),
    event(3,'turn_settled',{stop_reason:'end_turn'})],openSession());
  const tools=state.items.filter(item=>item.kind==='tool');
  assert.equal(tools.length,1);
  assert.equal(tools[0].tool,'echo_tool');
  assert.equal(tools[0].hostStatus,'completed');
  assert.ok(state.items.findIndex(item=>item.kind==='tool')>0);
});

test('a failed tool settlement stays a host fact and does not fail the turn',()=>{
  const state=reduce([{type:'turn_sending',text:'x'},
    event(1,'tool_call_started',{tool_call_id:'c1',host_status:'pending'}),
    event(2,'tool_call_settled',{tool_call_id:'c1',host_status:'failed'}),
    event(3,'message_completed',{})],openSession());
  assert.equal(state.items.find(item=>item.kind==='tool').hostStatus,'failed');
  assert.equal(state.error,null);
  assert.equal(canSend(state),true);
});

// --- questions -------------------------------------------------------------

test('a question is answerable by interaction id and disappears once settled',()=>{
  const asked=reduce([event(1,'question_requested',{interaction_id:'i1',question:'选一个',multi:false,
    options:[{option_id:'q1:1',label:'红色',description:''},{option_id:'q1:2',label:'蓝色',description:''}]})],openSession());
  assert.equal(pendingInteractions(asked).length,1);
  assert.equal(pendingInteractions(asked)[0].interactionId,'i1');
  assert.equal(pendingInteractions(asked)[0].options.length,2);
  const answered=agentReducer(asked,{type:'interaction_answered',interactionId:'i1',outcome:'selected'});
  assert.equal(pendingInteractions(answered).length,0);
  const settled=agentReducer(answered,event(2,'question_settled',{interaction_id:'i1',outcome:'selected',option_id:'q1:1'}));
  assert.equal(settled.interactions[0].status,'answered');
  assert.equal(settled.interactions[0].selectedOptionId,'q1:1');
  assert.equal(pendingInteractions(settled).length,0);
});

test('a permission request uses the same structure as a question',()=>{
  const asked=reduce([event(1,'permission_requested',{interaction_id:'p1',title:'run_skill',
    options:[{option_id:'perm:1',name:'Allow once',kind:'allow_once'}]})],openSession());
  assert.equal(pendingInteractions(asked)[0].kind,'permission');
  const settled=agentReducer(asked,event(2,'permission_settled',{interaction_id:'p1',outcome:'cancelled'}));
  assert.equal(settled.interactions[0].status,'cancelled');
});

test('a question cancelled with its turn stops being pending',()=>{
  const state=reduce([event(1,'question_requested',{interaction_id:'i1',question:'q',options:[]}),
    {type:'cancel_requested',accepted:true},
    event(2,'question_settled',{interaction_id:'i1',outcome:'cancelled'}),
    event(3,'message_cancelled',{})],openSession());
  assert.equal(pendingInteractions(state).length,0);
  assert.equal(state.turnStatus,TURN_STATUS.IDLE);
});

// --- ordering and reconnect ------------------------------------------------

test('a replayed event is dropped instead of duplicating the message',()=>{
  const once=reduce([event(1,'message_started',{message_id:'m1'}),
    event(2,'assistant_text_delta',{text:'你好',message_id:'m1'}),
    event(3,'turn_settled',{stop_reason:'end_turn'})],openSession());
  const replayed=reduce([event(1,'message_started',{message_id:'m1'}),
    event(2,'assistant_text_delta',{text:'你好',message_id:'m1'}),
    event(3,'turn_settled',{stop_reason:'end_turn'}),
    event(1,'message_started',{message_id:'m1'}),
    event(2,'assistant_text_delta',{text:'你好',message_id:'m1'}),
    event(3,'turn_settled',{stop_reason:'end_turn'})],openSession());
  assert.deepEqual(replayed.items,once.items);
  assert.equal(replayed.items.filter(item=>item.kind==='assistant').length,1);
  assert.equal(replayed.lastSequence,3);
});

test('an out of order event is ignored rather than rewinding the panel',()=>{
  const state=reduce([event(5,'assistant_text_delta',{text:'先',message_id:'m1'}),
    event(3,'assistant_text_delta',{text:'晚到的',message_id:'m1'}),
    event(6,'assistant_text_delta',{text:'后',message_id:'m1'})],openSession());
  assert.equal(state.items.find(item=>item.kind==='assistant').text,'先后');
  assert.equal(state.lastSequence,6);
});

test('events for an unknown type leave the state alone',()=>{
  const before=openSession();
  const after=agentReducer(before,event(1,'backend_event_ignored',{backend_type:'weird'}));
  assert.deepEqual(after.items,before.items);
  assert.equal(after.lastSequence,1);
});

// --- stop ------------------------------------------------------------------

test('stop ends the session and closes the composer',()=>{
  const stopping=agentReducer(openSession(),{type:'stop_requested'});
  assert.equal(stopping.sessionStatus,SESSION_STATUS.STOPPING);
  const stopped=reduce([event(1,'session_stopping',{}),event(2,'session_stopped',{})],openSession());
  assert.equal(stopped.sessionStatus,SESSION_STATUS.STOPPED);
  assert.equal(canSend(stopped),false);
  assert.equal(agentReducer(stopped,{type:'stop_settled'}).sessionStatus,SESSION_STATUS.STOPPED);
});

test('a stopped session is not a failed one',()=>{
  const stopped=reduce([event(1,'session_stopped',{})],openSession());
  assert.notEqual(stopped.sessionStatus,SESSION_STATUS.FAILED);
  assert.equal(stopped.error,null);
});

test('reset returns a fresh panel',()=>{
  const state=reduce([{type:'turn_sending',text:'x'},event(1,'assistant_text_delta',{text:'a'})],openSession());
  assert.deepEqual(agentReducer(state,{type:'reset'}),initialAgentState);
});
