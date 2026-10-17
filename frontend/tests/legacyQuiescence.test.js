// The old tag-conversation controls are removed from the tagging page while stored history and
// backend endpoints remain available to other maintenance paths.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
const backend=readFileSync(new URL('../../backend/app.py',import.meta.url),'utf8');

const LEGACY_UI=['标签对话','conversation-switcher','conversation-recycle','compatibility-note','ChatMessage','QuestionCard','/api/datasets/${did}/conversations'];

test('the tagging page exposes one Agent conversation entry and no retired chat controls',()=>{
  assert.ok(main.includes('<AgentPanel did={did}/>'),'the Agent is the sole conversation panel');
  for(const legacy of LEGACY_UI) assert.ok(!main.includes(legacy),'main.jsx must remove '+legacy);
});

test('old pending proposals remain actionable from the worksheet',()=>{
  assert.ok(main.includes('proposal-inbox'),'proposal inbox is rendered above worksheet dispatch');
  assert.ok(main.includes('proposalCard(currentProposal)'),'the selected proposal can still be reviewed');
  assert.ok(main.includes('otherPending.map(p=>proposalCard(p,true))'),'other pending proposals remain reachable');
});

test('conversation and attachment records remain stored behind the existing API',()=>{
  for(const route of ["@app.get('/api/datasets/{did}/conversations')",
    "@app.post('/api/datasets/{did}/conversations')",
    "@app.get('/api/datasets/{did}/conversations/{cid}/attachments/{aid}')"])
    assert.ok(backend.includes(route),'backend retains '+route);
});

test('the shared draft stream is still consumed',()=>{
  assert.match(main,/new EventSource\(`\/api\/drafts\/\$\{did\}\/events/);
  assert.ok(main.includes('applyDraftEvent(old,ev.kind,p)'),'workbench projection is still applied');
});
