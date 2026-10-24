import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
const panel=readFileSync(new URL('../src/agentPanel.jsx',import.meta.url),'utf8');
const sidebarCss=readFileSync(new URL('../src/projectSidebar.css',import.meta.url),'utf8');

test('dataset pending proposals are shown inside the Agent conversation',()=>{
  assert.ok(main.includes('selectPending(data?.proposals,did)'),
    'the screen uses the tested dataset-scoped selector');
  assert.ok(!main.includes("p.conversation_id===currentConversation"),
    'Agent proposals are not tied to legacy conversation IDs');
  assert.ok(main.includes('<AgentPanel did={did} proposalInbox={proposalInbox}/>' ),
    'the shared draft proposal is passed to the Agent panel');
  assert.ok(panel.includes('{proposalInbox}'),
    'the Agent panel displays the approval card');
  assert.ok(main.includes('if(currentProposal){setAgentMode(true);setFocus(false)}'),
    'an arriving proposal restores the Agent conversation when another view was open');
  assert.ok(main.includes('disabled={!!currentProposal} title={currentProposal?'),
    'the approval cannot be hidden by switching to a different panel');
  assert.ok(!main.split('<section className="sheet-panel">')[1]?.includes('proposal-inbox'),
    'approval placement must not depend on a worksheet-only inbox');
  assert.ok(main.includes("currentProposal?' has-agent-approval':''"),
    'the pending state marks the layout for narrow screens');
  assert.ok(sidebarCss.includes('.workspace.has-agent-approval>.conversation{display:flex}'),
    'narrow screens keep the Agent approval visible');
});
