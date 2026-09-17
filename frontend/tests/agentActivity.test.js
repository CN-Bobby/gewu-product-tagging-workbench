// Turn activity projection: one card per turn instead of one chat message per tool event.
//
// Every case replays the two inputs the panel really receives — application events in sequence order
// — and asserts on what the transcript looks like afterwards. The point is the invariants a
// projection like this can get wrong on its own: a tool event that appends a chat node, a second card
// for the same turn, a recoverable failure that reaches the main timeline, a reply split in two by a
// tool event, or a fatal error hidden behind a card.
import test from 'node:test';
import assert from 'node:assert/strict';

import {agentReducer,initialAgentState,SESSION_STATUS} from '../src/agentState.js';
import {ACTIVITY_STATUS,activityView,activityVisible,displayName,secondsBetween} from '../src/agentActivity.js';

const event=(sequence,event_type,payload={})=>({type:'agent_event',event:{sequence,event_type,payload}});
const started=(sequence,id,tool,at)=>(event(sequence,'tool_call_started',
  {tool_call_id:id,tool,host_status:'pending',timestamp:at}));
const settled=(sequence,id,host,at,error)=>(event(sequence,'tool_call_settled',
  {tool_call_id:id,host_status:host,timestamp:at,...(error?{error}:{})}));
const at=seconds=>new Date(Date.UTC(2026,0,1,0,0,0,seconds*1000)).toISOString();

const openSession=()=>[
  {type:'session_creating'},{type:'session_created',sessionId:'s1'},
  {type:'session_started',sessionId:'s1',remoteSessionId:'remote-1'}];
const reduce=actions=>actions.reduce(agentReducer,initialAgentState);
const cards=state=>state.items.filter(item=>item.kind==='activity');
const assistants=state=>state.items.filter(item=>item.kind==='assistant');
const timelineNodes=state=>state.items.filter(item=>item.kind!=='activity');
const turn=(events)=>reduce([...openSession(),
  event(1,'message_started',{message_id:'m1',timestamp:at(0)}),...events]);

// --- T1 one card for a whole turn -----------------------------------------

test('T1 five tool events in one turn produce exactly one activity card',()=>{
  const events=[];
  ['read_worksheet','read_tasks','read_profile','query_scope','propose_changes'].forEach((tool,index)=>{
    events.push(started(index*2+2,'c'+index,tool,at(index*2)),settled(index*2+3,'c'+index,'completed',at(index*2+1)));
  });
  const state=turn([...events,event(20,'turn_settled',{stop_reason:'end_turn',timestamp:at(12)}),
                    event(21,'message_completed',{message_id:'m1',seconds:1.2,timestamp:at(12)})]);
  assert.equal(cards(state).length,1,'one card, not one node per tool event');
  assert.equal(cards(state)[0].tools.length,5);
  // the transcript holds the user turn's card and the reply node — never a tool node
  assert.deepEqual(timelineNodes(state).map(item=>item.kind),['assistant']);
  assert.equal(cards(state)[0].completedCount,5);
});

// --- T2 the card updates in place -----------------------------------------

test('T2 a later tool event updates the same card and adds no chat node',()=>{
  const first=turn([started(2,'a','read_worksheet',at(0)),settled(3,'a','completed',at(1))]);
  const before=cards(first)[0];
  const after=agentReducer(first,started(4,'b','read_tasks',at(2)));
  assert.equal(cards(after)[0].currentTool,'read_tasks','an in-flight call is the current action');
  assert.equal(cards(after)[0].id,before.id,'the card identity is stable');
  const second=agentReducer(after,settled(5,'b','completed',at(3)));
  assert.equal(cards(second).length,1);
  assert.equal(cards(second)[0].id,before.id,'the card identity is stable');
  assert.equal(cards(second)[0].currentTool,null,'a finished call is not the current action');
  assert.equal(second.items.length,first.items.length,'no chat node was appended');
  assert.equal(cards(second)[0].tools.length,2);
});

// --- T3 recoverable failure stays out of the main timeline -----------------

test('T3 a tool failure shows in the timeline, not as a chat message',()=>{
  const state=turn([
    started(2,'a','route_tagging_skill',at(0)),
    settled(3,'a','failed',at(1),'tool unavailable'),
    started(4,'b','route_tagging_skill',at(2)),
    settled(5,'b','completed',at(3)),
    event(6,'turn_settled',{stop_reason:'end_turn',timestamp:at(4)}),
    event(7,'message_completed',{message_id:'m1',seconds:4,timestamp:at(4)})]);
  assert.deepEqual(timelineNodes(state).map(item=>item.kind),['assistant'],'no failure chat node');
  assert.equal(state.error,null,'a tool failure is not a turn error');
  const view=activityView(cards(state)[0],{expand:true});
  assert.equal(view.lines.length,2,'both executions are kept');
  assert.equal(view.lines[0].status,'failed');
  assert.equal(view.lines[0].glyph,'⚠');
  assert.equal(view.lines[0].error,'tool unavailable');
  assert.equal(view.lines[0].note,'自动重试');
  assert.equal(view.retryCount,1);
  assert.match(view.headline,/1 次自动重试/);
  assert.equal(view.collapsed,false);
});

// --- T4 questions stay interactive ----------------------------------------

test('T4 a question keeps its own card and is never swallowed by the activity card',()=>{
  const state=turn([
    started(2,'a','read_worksheet',at(0)),
    event(3,'question_requested',{interaction_id:'q1',question:'按什么维度分？',multi:false,
      options:[{option_id:'q1:1',label:'A'}],timestamp:at(1)}),
    event(4,'question_settled',{interaction_id:'q1',outcome:'selected',option_id:'q1:1',timestamp:at(2)}),
    settled(5,'a','completed',at(3))]);
  assert.equal(state.interactions.length,1);
  assert.equal(state.interactions[0].status,'answered');
  assert.equal(cards(state).length,1,'the question did not become a second activity card');
  assert.equal(cards(state)[0].waiting,false);
  assert.equal(cards(state)[0].resumeHint,true);
});

// --- T5 a settled turn collapses the card ---------------------------------

test('T5 turn_settled ends the card, and the copy reports operations and seconds',()=>{
  const state=turn([
    started(2,'a','read_worksheet',at(0)),settled(3,'a','completed',at(1)),
    event(4,'turn_settled',{stop_reason:'end_turn',timestamp:at(8.4)}),
    event(5,'message_completed',{message_id:'m1',seconds:8.4,timestamp:at(8.4)})]);
  const card=cards(state)[0];
  assert.equal(card.status,ACTIVITY_STATUS.DONE);
  assert.equal(card.seconds,8.4);
  const view=activityView(card);
  assert.equal(view.running,false);
  assert.equal(view.collapsed,true,'collapsed by default');
  assert.equal(view.headline,'已完成 · 1 个操作 · 8.4 秒');
  assert.equal(activityView(card,{expand:true}).collapsed,false,'the preference can open it');
  assert.equal(activityVisible(card),true);
});

// --- T6 the streaming reply keeps one node --------------------------------

test('T6 tool events never split or move the streaming reply node',()=>{
  const state=turn([
    event(2,'assistant_text_delta',{text:'先看',message_id:'m1',timestamp:at(0)}),
    started(3,'a','read_worksheet',at(0.2)),
    event(4,'assistant_text_delta',{text:'一下',message_id:'m1',timestamp:at(0.4)}),
    settled(5,'a','completed',at(0.5)),
    event(6,'assistant_text_delta',{text:'数据。',message_id:'m1',timestamp:at(0.6)})]);
  const reply=assistants(state);
  assert.equal(reply.length,1,'one reply node for the whole turn');
  assert.equal(reply[0].text,'先看一下数据。');
  assert.equal(reply[0].status,'streaming');
  assert.equal(cards(state).length,1);
  // the card sits above the reply it belongs to, so nothing below it ever moves
  assert.equal(state.items[0].kind,'activity');
  assert.equal(state.items[1].kind,'assistant');
});

// --- T7 history replay aggregates the same way ----------------------------

test('T7 replaying a stored history aggregates its tool events the same way',()=>{
  const stored=[...openSession(),
    event(1,'message_started',{message_id:'m1',timestamp:at(0)}),
    started(2,'a','read_worksheet',at(0)),settled(3,'a','completed',at(1)),
    started(4,'b','read_tasks',at(1)),settled(5,'b','completed',at(2)),
    started(6,'c','read_profile',at(2)),settled(7,'c','failed',at(3),'busy'),
    event(8,'message_completed',{message_id:'m1',seconds:3.4,timestamp:at(4)}),
    event(9,'message_started',{message_id:'m2',timestamp:at(10)}),
    started(10,'d','read_worksheet',at(10)),settled(11,'d','completed',at(11)),
    event(12,'message_completed',{message_id:'m2',seconds:1,timestamp:at(11)})];
  const state=reduce(stored);
  assert.equal(cards(state).length,2,'one card per stored turn, not one per tool event');
  assert.equal(cards(state)[0].tools.length,3);
  assert.equal(cards(state)[1].tools.length,1);
  assert.equal(cards(state)[0].retryCount,1);
  assert.equal(state.items.filter(item=>item.kind!=='activity'&&item.kind!=='assistant').length,0);
});

// --- T8 a fatal error is never hidden by the card --------------------------

test('T8 a fatal turn error stays in the main timeline',()=>{
  const state=turn([
    started(2,'a','read_worksheet',at(0)),settled(3,'a','failed',at(1),'blocked'),
    event(4,'message_failed',{message_id:'m1',fatal:true,timestamp:at(2)})]);
  assert.ok(state.error,'the error is reported to the user');
  assert.equal(state.error.fatal,true);
  assert.equal(cards(state)[0].status,ACTIVITY_STATUS.FAILED);
  const view=activityView(cards(state)[0]);
  assert.match(view.headline,/^执行失败/);
  assert.equal(view.hasFailures,true);
});

// --- T9 repeated failures of the same tool are kept as separate rows -------

test('T9 two failures with different call ids are two rows and two retries',()=>{
  const state=turn([
    started(2,'a','route_tagging_skill',at(0)),settled(3,'a','failed',at(1),'first'),
    started(4,'b','route_tagging_skill',at(2)),settled(5,'b','failed',at(3),'second'),
    event(6,'turn_settled',{stop_reason:'end_turn',timestamp:at(4)})]);
  const view=activityView(cards(state)[0],{expand:true});
  assert.equal(view.lines.length,2,'the two calls are not merged by tool name');
  assert.deepEqual(view.lines.map(line=>line.id),['a','b']);
  assert.equal(view.retryCount,2);
  assert.match(view.headline,/2 次自动重试/);
  assert.equal(view.completedCount,0);
});

// --- T10 cancel ------------------------------------------------------------

test('T10 cancel keeps one card and late events stay on it',()=>{
  const running=turn([started(2,'a','read_worksheet',at(0))]);
  const cancelling=agentReducer(running,{type:'cancel_requested',accepted:true});
  const view=activityView(cards(cancelling)[0]);
  assert.equal(view.running,true);
  assert.equal(view.headline,'正在取消…');
  const settledState=agentReducer(cancelling,event(3,'message_cancelled',{message_id:'m1',timestamp:at(1)}));
  assert.equal(cards(settledState)[0].status,ACTIVITY_STATUS.CANCELLED);
  assert.match(activityView(cards(settledState)[0]).headline,/^已取消/);
  // an event that arrives after the turn ended belongs to that turn, not to a ghost card
  const late=agentReducer(settledState,settled(4,'a','completed',at(2)));
  assert.equal(cards(late).length,1,'no ghost card');
  assert.equal(cards(late)[0].id,cards(settledState)[0].id);
  assert.equal(cards(late)[0].status,ACTIVITY_STATUS.CANCELLED,'a late frame cannot rewrite the outcome');
});

// --- projection helpers ----------------------------------------------------

test('tool names are shown as actions, and the raw name survives',()=>{
  assert.equal(displayName('read_worksheet'),'读取工作表');
  assert.equal(displayName('read_tasks'),'检查任务');
  assert.equal(displayName('route_tagging_skill'),'选择打标技能');
  assert.equal(displayName('read_profile'),'读取项目配置');
  assert.equal(displayName('view_product_image'),'查看商品图片');
  assert.equal(displayName('get_product_images'),'获取商品图片');
  assert.equal(displayName('fetch_product_metadata'),'获取商品信息');
  assert.equal(displayName('mcp__product_workbench__read_worksheet'),'读取工作表');
  assert.equal(displayName('some_unknown_thing'),'执行 some_unknown_thing','never a bare snake_case title');
  assert.equal(displayName(''),'执行操作');
  const view=activityView({status:ACTIVITY_STATUS.DONE,completedCount:1,retryCount:0,warningCount:0,seconds:1,
    tools:[{id:'a',tool:'get_product_images',status:'completed',duration:0.3}]});
  assert.deepEqual(view.lines.map(line=>({label:line.label,tool:line.tool,duration:line.duration})),
    [{label:'获取商品图片',tool:'get_product_images',duration:'0.3s'}],'the raw name travels with the row');
  assert.equal(secondsBetween(at(0),at(0.3)),0.3);
  assert.equal(secondsBetween(null,at(1)),null);
  assert.equal(activityVisible({status:ACTIVITY_STATUS.DONE,tools:[]}),false,'a tool-free turn adds no card');
  assert.equal(activityVisible({status:ACTIVITY_STATUS.RUNNING,tools:[]}),true,'but it holds its place while running');
});
