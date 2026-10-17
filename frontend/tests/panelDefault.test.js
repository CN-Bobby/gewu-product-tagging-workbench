// The default panel and the two boundaries the cutover must not cross.
//
// Checked at the source level because these are wiring facts a pure reducer cannot see: which panel
// the workbench opens with, that the new panel cannot reach the legacy chat commands, and that the
// shared draft stream is still consumed for workbench state. The behavioural half of each is
// covered by workbenchEvents.test.js and by the browser smoke.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=name=>readFileSync(new URL(`../src/${name}`,import.meta.url),'utf8');
const main=source('main.jsx');
const panel=source('agentPanel.jsx');

test('the tagging page renders only the Agent conversation panel',()=>{
  assert.ok(main.includes('<strong>Agent 会话</strong>'));
  assert.ok(main.includes('<AgentPanel did={did}/>'));
  assert.ok(!main.includes('agentMode'));
  assert.ok(!main.includes('标签对话'));
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

test('the workbench has home, tagging, keyword, Xunlongjue, and settings routes',()=>{
  assert.ok(main.includes("import {KeywordResearch} from './keywordResearch'"),'the page is imported');
  assert.ok(main.includes('routeFromHash'),'deep links resolve from the hash');
  assert.ok(main.includes('PAGE.XUNLONGJUE'),'the Xunlongjue route is accepted');
  assert.ok(main.includes('PAGE.AMAZON_VOC'),'the Amazon VOC route is accepted');
  assert.ok(main.includes("import {Xunlongjue} from './Xunlongjue'"),'the page is imported');
  assert.ok(main.includes('page===PAGE.XUNLONGJUE?<Xunlongjue'),'the Xunlongjue route renders the module');
  assert.ok(main.includes('page===PAGE.HOME')&&main.includes('page===PAGE.SETTINGS'),'home and settings render independently');
  assert.ok(main.includes('page===PAGE.KEYWORD?<KeywordResearch'),'keyword route renders the keyword page');
  assert.ok(main.includes('page===PAGE.TAGGING&&data&&showHistory'),'tagging drawers are scoped to tagging');
});

test('the topbar carries the entry to the keyword page',()=>{
  assert.ok(main.includes('关键词调研'),'the entry label is in the topbar');
  assert.match(main,/<button className=\{page===PAGE\.KEYWORD\?'active':''\} onClick=\{\(\)=>navigate\('keyword'\)\}>关键词调研<\/button>/,'the entry switches the page');
});

test('Product Archive has a separate dossier route and read-only sample page',()=>{
  const page=source('productArchive.jsx');
  assert.ok(main.includes("import {ProductArchive} from './productArchive'"));
  assert.ok(main.includes("parts[0]==='product-archive'"));
  assert.ok(main.includes("page==='product-archive'?<ProductArchive"));
  assert.ok(main.includes("navigate('product-archive')"));
  assert.ok(page.includes('PRODUCT / MARKET OVERVIEW'));
  assert.ok(page.includes('MARKET SIZE'));
  assert.ok(page.includes('COMPETITIVE STRUCTURE'));
  assert.ok(page.includes('READ ONLY PREVIEW'));
  assert.ok(page.includes('确认映射并生成快照'));
});

test('the topbar carries the entry to the Xunlongjue module',()=>{
  assert.ok(main.includes('寻龙诀'),'the entry label is in the topbar');
  assert.match(main,/<button className=\{page===PAGE\.XUNLONGJUE\?'active':''\} onClick=\{\(\)=>navigate\('xunlongjue'\)\}>寻龙诀<\/button>/,'the entry switches to the module');
});

test('the topbar opens one Amazon Reviews and VOC workspace',()=>{
  assert.ok(main.includes("import {AmazonReviewsVoc} from './amazonReviewsVoc'"));
  assert.match(main,/<button className=\{\['amazon-reviews','amazon-reviews-voc',PAGE\.AMAZON_VOC\]\.includes\(page\)\?'active':''\} onClick=\{\(\)=>navigate\('amazon-reviews-voc'\)\}>Amazon 评论 \+ VOC<\/button>/);
  assert.ok(main.includes("['amazon-reviews','amazon-reviews-voc',PAGE.AMAZON_VOC].includes(page)?<AmazonReviewsVoc"));
});

test('VOC deep links parse an exact run and pass it into the VOC page',()=>{
  assert.ok(main.includes("import {parseAmazonVocRoute} from './amazonReviewsState'"));
  assert.ok(main.includes('amazonVocRoute=parseAmazonVocRoute(location.hash)'));
  assert.ok(main.includes('onNavigate={(path,message)=>{setVocHandoffNotice(message);navigate(path)}}'));
  assert.ok(main.includes('initialNotice={vocHandoffNotice} onNoticeConsumed={setVocHandoffNotice}'));
  const voc=source('amazonVoc.jsx');
  assert.match(voc,/export function AmazonVOC\(\{api,initialRunId='',initialNotice='',onNoticeConsumed,workspaceId='',forceRefresh=false\}\)/);
  assert.ok(voc.includes('ENDPOINTS.run(initialRunId)'));
  assert.ok(voc.includes('runRef.current=linkedRun.id;setRunId(linkedRun.id)'));
  assert.ok(voc.includes("setNotice(initialNotice);onNoticeConsumed?.('')"));
});

test('Amazon Reviews requires an explicit reviewed-workbook handoff and follows its returned run',()=>{
  const reviews=source('amazonReviews.jsx');
  assert.ok(reviews.includes('isAmazonReviewsVocEligible(selected)'));
  assert.ok(reviews.includes('我已核对 Excel，确认并启动 VOC'));
  assert.ok(reviews.includes('api(request.path,request.body)'));
  assert.ok(reviews.includes('setJobs(items=>items.map(job=>job.id===selected.id?{...job,...value.source_job}:job))'));
  assert.ok(reviews.includes('onNavigate?.(amazonVocRunHash(value.run.id),message)'));
  assert.ok(reviews.includes('只分析已成功导出的评论，不包含失败 ASIN'));
});
