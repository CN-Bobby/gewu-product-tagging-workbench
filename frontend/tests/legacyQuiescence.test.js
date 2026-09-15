// The legacy panel is a viewer: history stays, the commands that would start an old Agent turn
// are gone.
//
// Checked at the source level because the thing being asserted is the absence of a call site — no
// request can be issued from a code path that does not exist. The behavioural half (nothing is
// requested when the panel is switched to, and the history still renders) is covered by the real
// browser smoke; the shared draft-stream projection has its own suite and is deliberately
// untouched here.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');

// Commands that reach the legacy conversation controller. The backend routes still exist; nothing
// in this UI may call them.
const RUNTIME_COMMANDS=['/api/datasets/${did}/chat','/chat/stop','chat/questions','chat/stop'];

test('the legacy viewer exposes no way to start, stop or answer a legacy turn',()=>{
  for(const command of RUNTIME_COMMANDS)
    assert.ok(!main.includes(command),'main.jsx must not call '+command);
  // the helpers that used to do it are gone rather than merely disabled
  for(const helper of ['const send=','const answerQuestion=','const uploadChatFiles=','const addChatFiles='])
    assert.ok(!main.includes(helper),'main.jsx must not define '+helper);
  assert.ok(!main.includes('className="composer"'),'the legacy composer is gone');
  assert.ok(!main.includes('handle(send)'),'no control submits a legacy message');
});

test('the viewer says what it is instead of pretending to send',()=>{
  assert.ok(main.includes('compatibility-note'),'the notice is rendered');
  assert.ok(main.includes('旧版标签对话仅用于查看历史记录'),'the notice names the change');
  assert.ok(main.includes('Agent 会话'),'and points at the new panel');
});

test('the history is still rendered',()=>{
  // the message list, the questions attached to a message, and the presentation all survive
  assert.ok(main.includes('<ChatMessage'),'messages are still rendered');
  assert.ok(main.includes('{activeMessages.map(m=><ChatMessage'),
    'the message list is rendered, not merely referenced');
  assert.ok(main.includes("(data.questions||[]).filter"),'questions are still attached to messages');
  assert.ok(main.includes('presentation'),'the presentation is still rendered');
  assert.ok(main.includes('readOnly'),'the viewer passes its read-only mode down');
});

test('the thread and attachment controls are untouched',()=>{
  // business data, not orchestration: these keep working
  for(const kept of ['/api/datasets/${did}/conversations','attachments','bindAttachment'])
    assert.ok(main.includes(kept),'main.jsx must keep '+kept);
});

test('the shared draft stream is still consumed',()=>{
  assert.match(main,/new EventSource\(`\/api\/drafts\/\$\{did\}\/events/);
  assert.ok(main.includes('applyDraftEvent(old,ev.kind,p)'),'workbench projection is still applied');
});
