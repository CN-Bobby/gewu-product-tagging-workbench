// The default panel and the two boundaries the cutover must not cross.
//
// Checked at the source level because these are wiring facts a pure reducer cannot see: which panel
// the workbench opens with, that the new panel cannot reach the legacy chat commands, and that the
// shared draft stream is still consumed for workbench state. The behavioural half of each is
// covered by workbenchEvents.test.js and by the browser smoke.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {DEFAULT_PANEL,PANEL} from '../src/agentState.js';

const source=name=>readFileSync(new URL(`../src/${name}`,import.meta.url),'utf8');
const main=source('main.jsx');
const panel=source('agentPanel.jsx');

test('the workbench opens on the new agent panel',()=>{
  assert.equal(DEFAULT_PANEL,PANEL.AGENT);
  assert.equal(PANEL.AGENT,'agent');
});

test('the panel mode is initialised from that default rather than a literal',()=>{
  assert.match(main,/useState\(DEFAULT_PANEL===PANEL\.AGENT\)/);
  assert.ok(main.includes('import {DEFAULT_PANEL,PANEL} from'),'the default must come from the module');
});

test('the legacy chat is still reachable as an explicit fallback',()=>{
  // both titles and the toggle exist, and the legacy subtree is still rendered in the else branch
  assert.ok(main.includes('Agent 会话'),'the agent panel label');
  assert.ok(main.includes('标签对话'),'the fallback label');
  assert.match(main,/\{agentMode\?<AgentPanel did=\{did\}\/>:<>/,'the fallback subtree is intact');
});

test('the new panel cannot reach the legacy chat commands',()=>{
  for(const legacy of ['/chat','chat/stop','chat/questions','/api/datasets/'])
    assert.ok(!panel.includes(legacy),'agentPanel must not reference '+legacy);
  assert.ok(panel.includes('/api/agent/sessions'),'the panel speaks only the agent API');
});

test('the shared draft stream is still consumed for workbench state',()=>{
  assert.match(main,/new EventSource\(`\/api\/drafts\/\$\{did\}\/events/,'the draft stream is still opened');
  const handler=main.split('events.onmessage=')[1].split('\n')[0];
  assert.ok(handler.includes('setData(old=>applyDraftEvent(old,ev.kind,p))'),
    'workbench state is still projected from draft events');
  // and projected unconditionally: gating it on the panel mode would strand the worksheet whenever
  // the agent panel is the active one, which is now the default.
  assert.ok(!/if\([^)]*\)\s*setData\(old=>applyDraftEvent/.test(handler),
    'the projection must not be conditional');
  assert.ok(!main.includes("if(ev.kind==='chat_chunk')return"),'the projection is no longer inlined');
});

test('the workbench has home, tagging, keyword, and settings routes',()=>{
  assert.ok(main.includes("import {KeywordResearch} from './keywordResearch'"),'the page is imported');
  assert.ok(main.includes('routeFromHash'),'deep links resolve from the hash');
  assert.ok(main.includes('page===PAGE.HOME')&&main.includes('page===PAGE.SETTINGS'),'home and settings render independently');
  assert.ok(main.includes('page===PAGE.KEYWORD?<KeywordResearch'),'keyword route renders the keyword page');
  assert.ok(main.includes('page===PAGE.TAGGING&&data&&showHistory'),'tagging drawers are scoped to tagging');
});

test('the topbar carries the entry to the keyword page',()=>{
  assert.ok(main.includes('关键词调研'),'the entry label is in the topbar');
  assert.match(main,/<button className=\{page===PAGE\.KEYWORD\?'active':''\} onClick=\{\(\)=>navigate\('keyword'\)\}>关键词调研<\/button>/,'the entry switches the page');
});
