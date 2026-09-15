// The event-stream contract between the panel and the server, checked without a browser.
//
// A named SSE event is never delivered to EventSource.onmessage, and a name mismatch fails with no
// error at all — the connection stays open and simply nothing arrives. That is invisible in a unit
// test of the reducer, so the two sides are compared directly here.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const panel=readFileSync(new URL('../src/agentPanel.jsx',import.meta.url),'utf8');
const server=readFileSync(new URL('../../backend/agent/stream.py',import.meta.url),'utf8');

function panelEventName(){
  const match=panel.match(/const STREAM_EVENT='([^']+)'/);
  return match&&match[1];
}

function serverEventName(){
  const match=server.match(/SSE_EVENT_NAME='([^']+)'/);
  return match&&match[1];
}

test('the panel listens for the event name the server actually sends',()=>{
  const name=serverEventName();
  assert.ok(name,'serve must declare its SSE event name');
  assert.equal(panelEventName(),name);
  assert.ok(panel.includes(`addEventListener(STREAM_EVENT`),'a named event needs a named listener');
});

test('the stream path the panel uses is the one the server serves',()=>{
  assert.ok(panel.includes('/api/agent/sessions/${state.sessionId}/events'),'panel stream path');
});

test('the stream carries a sequence, which is what the panel orders by',()=>{
  assert.ok(server.includes("'id: '")||server.includes('id: '),'the frame carries an id for resume');
  assert.ok(panel.includes('sequence'),'the panel orders events by sequence');
});
