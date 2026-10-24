// The old tag conversation is a read-only history view. New proposals and approvals belong to Agent.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
const backend=readFileSync(new URL('../../backend/app.py',import.meta.url),'utf8');

test('the tagging page defaults to Agent and keeps old history read-only',()=>{
  assert.ok(main.includes('useState(DEFAULT_PANEL===PANEL.AGENT)'));
  assert.ok(main.includes('<AgentPanel did={did} proposalInbox={proposalInbox}/>'));
  assert.ok(main.includes('旧版标签对话仅用于查看历史记录。'));
  assert.ok(main.includes('onBind={a=>handle(()=>bindAttachment(a))} readOnly/>'));
});

test('pending proposals remain actionable from the Agent conversation',()=>{
  assert.ok(main.includes('<AgentPanel did={did} proposalInbox={proposalInbox}/>'),
    'the Agent conversation receives the proposal inbox');
  assert.ok(main.includes('proposal-inbox'),'the proposal inbox is rendered in the conversation');
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
