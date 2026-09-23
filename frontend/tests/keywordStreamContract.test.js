// The event-stream and route contract between the keyword research page and the server, checked
// without a browser.
//
// A named SSE event is never delivered to EventSource.onmessage, and a name mismatch fails with no
// error at all — the connection stays open and simply nothing arrives. That is invisible in a unit
// test of the reducer, so both sides are compared directly here: the frontend constants against the
// Python modules that send the frames and declare the routes, and the frontend path templates
// against the FastAPI decorators they call.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import{API_PREFIX,ENDPOINTS,RUN_EVENT,RUN_EVENT_TYPES,SSE_EVENT_NAME,TERMINAL_EVENT_TYPES}
  from'../src/keywordState.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const state=read('../src/keywordState.js');
const page=read('../src/keywordResearch.jsx');
const streamModule=read('../../backend/keyword_research/stream.py');
const runnerModule=read('../../backend/keyword_research/runner.py');
const registryModule=read('../../backend/keyword_research/registry.py');
const routesModule=read('../../backend/keyword_research/routes.py');

function pythonConstant(source,name){
  const match=source.match(new RegExp(name+'\\s*=\\s*[\'"]([^\'"]+)[\'"]'));
  return match&&match[1];
}

function pythonStringSet(source,name){
  const match=source.match(new RegExp(name+'\\s*=\\s*frozenset\\(\\{([^}]*)\\}\\)'));
  if(!match)return null;
  return [...match[1].matchAll(/'([^']+)'/g)].map(item=>item[1]).sort();
}

// One shape for both sides: a path parameter is a parameter, whatever the language calls it, and
// the query string is not part of the path.
const normalize=value=>value.split('?')[0].replace(/\$\{[^}]+\}/g,'{param}').replace(/\{[^}]+\}/g,'{param}');

// The paths the page can call, read from the one table that builds them.
function frontendPaths(){
  const found=[];
  const pattern=/`\$\{API_PREFIX\}([^`]*)`/g;
  let match=pattern.exec(state);
  while(match){found.push(normalize(API_PREFIX+match[1]));match=pattern.exec(state)}
  return found;
}

function declaredRoutes(){
  const prefix=(routesModule.match(/APIRouter\(prefix='([^']+)'\)/)||[])[1];
  assert.ok(prefix,'routes.py must declare its prefix');
  const routes=[];
  const pattern=/@router\.(get|post|delete|put|patch)\('([^']+)'\)/g;
  let match=pattern.exec(routesModule);
  while(match){routes.push(normalize(prefix+match[2]));match=pattern.exec(routesModule)}
  return routes;
}

test('the page listens for the event name the server actually sends',()=>{
  const name=pythonConstant(streamModule,'SSE_EVENT_NAME');
  assert.ok(name,'stream.py must declare its SSE event name');
  assert.equal(SSE_EVENT_NAME,name);
  assert.ok(page.includes('addEventListener(SSE_EVENT_NAME'),'a named event needs a named listener');
  assert.ok(!/onmessage\s*=/.test(page),'onmessage never sees a named event, so none is installed');
});

test('the page and the server agree on which event types end a run',()=>{
  const server=pythonStringSet(streamModule,'TERMINAL_EVENT_TYPES');
  assert.ok(server,'stream.py must declare its terminal event types');
  assert.deepEqual([...TERMINAL_EVENT_TYPES].sort(),server);
});

test('every event type the page understands is one the runner sends',()=>{
  const types=RUN_EVENT_TYPES;
  assert.equal(new Set(types).size,types.length,'a duplicated type would be counted twice');
  for(const eventType of types){
    const sender=eventType==='run_skipped'?registryModule:runnerModule;
    assert.ok(sender.includes("'"+eventType+"'"),eventType==='run_skipped'?'registry.py must record skipped queue events':'runner.py must send '+eventType);
  }
  for(const eventType of TERMINAL_EVENT_TYPES)
    assert.ok(streamModule.includes("'"+eventType+"'"),'stream.py must end the stream on '+eventType);
  assert.equal(RUN_EVENT.STAGE_PROGRESS,'stage_progress');
  assert.equal(TERMINAL_EVENT_TYPES.includes(RUN_EVENT.LOG),false,'a log line does not end a run');
});

test('every path the page can call is a route the server declares',()=>{
  const routes=declaredRoutes();
  const called=frontendPaths();
  assert.equal(called.length,15,'the endpoint table must declare every path the page uses');
  for(const path of called)
    assert.ok(routes.includes(path),'the server does not declare '+path);
  // and nothing on this page spells a keyword research path out by hand
  assert.ok(!page.includes(API_PREFIX),'the page must go through the endpoint table');
});

test('the stream is opened on the declared path with the cursor the page resumed from',()=>{
  assert.ok(page.includes('new EventSource(ENDPOINTS.runEvents(state.runId'),
    'the run stream is opened from the shared endpoint table');
  assert.ok(ENDPOINTS.runEvents('r1',7).endsWith('?after_sequence=7'),'the cursor travels in the query');
  assert.ok(page.includes('ENDPOINTS.run('),'the first paint and every reconnect read the snapshot');
});
