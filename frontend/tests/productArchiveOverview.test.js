import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  applyGeneratedAnalysis,
  addOverviewItem,
  createDefaultOverviewModules,
  createOverviewModule,
  moveOverviewModule,
  removeOverviewItem,
  removeOverviewModule,
  updateOverviewItem,
  updateOverviewModule,
  workflowKeywordTerms,
  isCurrentPendingRun,
} from '../src/productArchiveOverview.js';

test('an older unreviewed draft does not hide a newer saved analysis', () => {
  const older = { id: 'old', status: 'ready', created_at: '2026-10-21T12:00:00Z' };
  const saved = { id: 'saved', created_at: '2026-10-24T12:00:00Z' };
  assert.equal(isCurrentPendingRun(older, 'saved', saved), false);
  assert.equal(isCurrentPendingRun({ ...older, created_at: '2026-10-25T12:00:00Z' }, 'saved', saved), true);
  assert.equal(isCurrentPendingRun(older, '', null), true);
  assert.equal(isCurrentPendingRun({ ...older, created_at: '' }, 'saved', saved), true);
  assert.equal(isCurrentPendingRun({ ...older, review_decision: 'accepted' }, '', null), false);
});

// This checkout has no DOM/component test runtime. These tests stay offline:
// they exercise the pure module helpers with synthetic results and inspect the
// UI source for the API-backed states that still need browser acceptance.
const archivePage = readFileSync(new URL('../src/productArchive.jsx', import.meta.url), 'utf8');
const requireUiContract = (pattern, label) => assert.ok(pattern.test(archivePage), `UI contract missing: ${label}`);
const rejectUiContract = (pattern, label) => assert.ok(!pattern.test(archivePage), `UI contract violated: ${label}`);

function createFakeApi(fixtures) {
  const calls = [];
  const api = async (path, body, method = 'GET') => {
    calls.push({ path, body, method });
    if (!path.startsWith('/api/product-archive/')) throw new Error('fake API blocks non-archive endpoints');
    const fixture = fixtures[path];
    if (!fixture?.length) throw new Error(`no fake response configured for ${path}`);
    return structuredClone(fixture.shift());
  };
  return { api, calls };
}

test('default modules preserve current product narrative and never seed keywords from model overview text', () => {
  const modules = createDefaultOverviewModules({
    overview: {
      product_function: '完整的产品使用描述。',
      target_audience: '旧有人群描述。',
      representative_keywords: '模型推测的词',
    },
    integrations: { keyword_research: {
      sources: [{ run_id: 'run-1', keyword_count: 1 }],
      representative_terms: [{ keyword: 'task term', source_run_id: 'run-1' }],
    } },
  });
  assert.equal(modules.find((module) => module.field_key === 'product_function').items[0].text, '完整的产品使用描述。');
  assert.equal(modules.some((module) => module.field_key === 'representative_keywords'), false);
  assert.deepEqual(workflowKeywordTerms({
    overview_suggestions: { representative_keywords: [{ text: 'model term' }] },
    integrations: { keyword_research: {
      sources: [{ run_id: 'run-1', keyword_count: 1 }],
      representative_terms: [
        { keyword: 'task term', source_run_id: 'run-1' },
        { keyword: 'orphan term', source_run_id: 'unfinished-run' },
      ],
    } },
  }).map((item) => item.keyword), ['task term']);
});

test('persisted custom modules refresh keyword cards only from completed workflow runs', () => {
  const modules = createDefaultOverviewModules({
    overview_modules: [{ id: 'custom-a', title: '自定义', field_key: null, items: [] }],
    integrations: { keyword_research: {
      sources: [{ run_id: 'done', keyword_count: 2 }],
      representative_terms: [{ keyword: 'verified term', source_run_id: 'done' }],
    } },
  });
  assert.equal(modules[0].id, 'custom-a');
  assert.equal(modules[1].read_only, true);
  assert.equal(modules[1].items[0].text, 'verified term');
});

test('new archive with an empty module list still shows all narrative modules and preserves legacy text', () => {
  const archive = {
    overview_modules: [],
    overview: { buying_motives: '人工写下的需求说明。' },
    integrations: { keyword_research: {
      sources: [{ run_id: 'done', keyword_count: 1 }],
      representative_terms: [{ keyword: 'task term', source_run_id: 'done' }],
    } },
  };
  const modules = createDefaultOverviewModules(archive);
  assert.deepEqual(modules.slice(0, 7).map((module) => module.field_key), [
    'product_name', 'product_function', 'target_audience', 'usage_scenarios',
    'buying_motives', 'cultural_context', 'seasonality',
  ]);
  assert.equal(modules.find((module) => module.field_key === 'buying_motives').items[0].text,
    '人工写下的需求说明。');
  assert.equal(modules.at(-1).field_key, 'workflow_keywords');
  const filled = applyGeneratedAnalysis(modules, {
    product_function: [{ id: 'function-1', text: '综合功能描述。' }],
    usage_scenarios: [{ id: 'scenario-1', text: '细分场景描述。' }],
  });
  assert.equal(filled.find((module) => module.field_key === 'product_function').items.length, 1);
  assert.equal(filled.find((module) => module.field_key === 'usage_scenarios').items.length, 1);
});

test('an explicitly saved empty layout remains empty', () => {
  assert.deepEqual(createDefaultOverviewModules({
    overview_modules: [], overview_modules_initialized: true,
  }), []);
});

test('one whole analysis adds narrative and repeatable audience and scenario items without fixed decomposition', () => {
  const modules = applyGeneratedAnalysis([], {
    product_function: [{ id: 'function-1', text: '面向家庭，在泳池或海滩帮助儿童完成水上活动。', source_type: 'overview_draft', source_ids: ['e1'] }],
    target_audience: [{ id: 'audience-1', text: 'S1 家庭户外活动：关注日晒与穿脱。', source_type: 'overview_draft', source_ids: ['e2'] }],
    usage_scenarios: [{ id: 'scene-1', text: '泳池课程：固定训练后需快速换装。', source_type: 'overview_draft', source_ids: ['e3'] }],
    representative_keywords: [{ id: 'unsafe', text: 'model generated keyword', source_type: 'overview_draft' }],
  });
  assert.equal(modules.find((module) => module.field_key === 'product_function').items[0].text, '面向家庭，在泳池或海滩帮助儿童完成水上活动。');
  assert.equal(modules.find((module) => module.field_key === 'target_audience').items[0].text, 'S1 家庭户外活动：关注日晒与穿脱。');
  assert.equal(modules.find((module) => module.field_key === 'usage_scenarios').items[0].text, '泳池课程：固定训练后需快速换装。');
  assert.equal(modules.some((module) => module.items.some((item) => item.id === 'unsafe')), false);
});

test('generated conclusions retain segment, inference, evidence and boundary metadata', () => {
  const modules = createDefaultOverviewModules();
  const result = applyGeneratedAnalysis(modules, {
    buying_motives: [{ id: 'motive-1', text: '减少户外日晒顾虑。', segment_id: 'S1', segment_name: '亲子户外家庭',
      segment_classification: 'hypothesis', basis: 'inference', basis_note: '由场景和用户任务综合判断。', limits: '需验证购买表达。', source_ids: [] }],
  });
  const motive = result.find((item) => item.field_key === 'buying_motives').items[0];
  assert.equal(motive.segment_id, 'S1');
  assert.equal(motive.segment_name, '亲子户外家庭');
  assert.equal(motive.basis, 'inference');
  assert.equal(motive.limits, '需验证购买表达。');
  assert.equal(motive.segment_classification, 'hypothesis');
});

test('ready result shows each segment conclusion support tier and partial research failure counts', () => {
  requireUiContract(/segment_classification|细分性质/, 'support tier next to each conclusion');
  requireUiContract(/档案中有线索/, 'archive clue tier without overstating validation');
  requireUiContract(/不代表细分人群规模或需求已被验证/, 'scope of archive evidence');
  requireUiContract(/page_fetch_failures|网页读取失败/, 'visible partial research failure count');
  requireUiContract(/quality_warnings|描述偏短/, 'visible concise-content quality warning');
});

test('conclusion evidence names are readable instead of exposing internal source identifiers', () => {
  requireUiContract(/sourceDisplayName\(id,\s*overviewRun\)/, 'source ids resolve to names in the result manifest');
  requireUiContract(/source_manifest\?\.find/, 'source display helper reads the result source manifest');
});

test('search coverage explains common technical failures in plain language', () => {
  requireUiContract(/failure_code/, 'safe search failure category is rendered');
  requireUiContract(/搜索服务暂时限制/, 'challenge or rate limit is understandable');
  requireUiContract(/连接超时/, 'network timeout is understandable');
});

test('custom modules can be added and removed without changing the remaining module content', () => {
  const initial = createDefaultOverviewModules({ overview: { product_function: '原描述' } });
  const custom = createOverviewModule('新的细分人群', 'custom-a');
  const withCustom = [...initial, custom];
  assert.equal(withCustom.at(-1).title, '新的细分人群');
  assert.equal(removeOverviewModule(withCustom, 'custom-a').some((module) => module.id === 'custom-a'), false);
  assert.equal(withCustom.find((module) => module.field_key === 'product_function').items[0].text, '原描述');
});

test('module titles, entries and order can be changed without imposing a fixed analysis taxonomy', () => {
  const modules = createDefaultOverviewModules({ overview: { product_function: '综合功能描述' } });
  const functionModule = modules.find((module) => module.field_key === 'product_function');
  let edited = updateOverviewModule(modules, functionModule.id, { title: '新的产品总结' });
  edited = updateOverviewItem(edited, functionModule.id, functionModule.items[0].id, { text: '用户改写后的总结' });
  edited = addOverviewItem(edited, functionModule.id, { text: '另一个场景描述' });
  edited = removeOverviewItem(edited, functionModule.id, functionModule.items[0].id);
  edited = moveOverviewModule(edited, functionModule.id, -1);
  assert.equal(edited[0].title, '新的产品总结');
  assert.deepEqual(edited[0].items.map((item) => item.text), ['另一个场景描述']);
  assert.equal(edited.find((module) => module.field_key === 'product_function').items[0].text, '另一个场景描述');
});

test('analysis progress explains the stage and bounded public research and model-call ceilings', () => {
  requireUiContract(/overview-progress-stage|分析阶段/, 'readable progress stage');
  requireUiContract(/reasoning_model_ref|reasoning_name/, 'text reasoning model distinct from image model');
  requireUiContract(/模型请求 3 次|最多 3 次模型请求/, 'three-stage model-call ceiling');
  requireUiContract(/最多 6 次搜索/ , 'bounded two-round search ceiling');
  requireUiContract(/12 篇网页/, 'bounded two-round page ceiling');
  requireUiContract(/public_research:true/, 'public research is enabled by default');
});

test('research trace shows evidence-backed findings between first-round search and follow-up', () => {
  requireUiContract(/plan\.findings/, 'visible first-round findings');
  requireUiContract(/首轮发现（基于已读资料）/, 'plain-language finding section');
  requireUiContract(/item\.evidence_ids[^}]*sourceDisplayName/, 'finding sources resolved to readable names');
});

test('fake API lifecycle fixture stays local and records only the expected draft status requests', async () => {
  const archiveId = 'archive-fixture';
  const runId = 'run-fixture';
  const path = `/api/product-archive/archives/${archiveId}/overview-drafts/${runId}`;
  const { api, calls } = createFakeApi({ [path]: [
    { id: runId, status: 'running', stage: 'model_analysis', model_calls: 1, web_search_calls: 0 },
    { id: runId, status: 'ready', suggestions: { product_function: [{ id: 'suggestion-a', text: 'Synthetic result' }] },
      usage: { usage_quality: 'unknown', estimated_cost: null } },
  ] });
  assert.equal((await api(path, undefined, 'GET')).status, 'running');
  const ready = await api(path, undefined, 'GET');
  assert.equal(ready.status, 'ready');
  assert.equal(ready.usage.usage_quality, 'unknown');
  assert.equal(ready.usage.estimated_cost, null);
  assert.deepEqual(calls.map((call) => call.method), ['GET', 'GET']);
  assert.ok(calls.every((call) => call.path.startsWith('/api/product-archive/')));
});

test('ready analysis previews claims, inference basis, source links and usage before the whole-analysis decision', () => {
  requireUiContract(/overview-suggestion-preview|建议预览/, 'ready-state suggestion preview');
  requireUiContract(/suggestion\.text|item\.text/, 'suggestion content');
  requireUiContract(/对照资料：\{suggestion\.source_ids\.map\(id=>sourceDisplayName\(id,overviewRun\)\)/, 'pending suggestions show human-readable source names');
  requireUiContract(/basis_note|结论性质/, 'observation/inference basis');
  requireUiContract(/suggestion\.links|item\.links|支持来源/, 'source evidence links');
  requireUiContract(/\.quality===|usage_quality|usageQuality/, 'usage quality');
  requireUiContract(/费用状态|费用未知|estimated_cost/, 'cost state');
  requireUiContract(/保留整份分析/, 'whole-analysis keep action');
  requireUiContract(/忽略整份分析/, 'whole-analysis ignore action');
});

test('whole keep and ignore decisions leave editable, addable and removable cards available', () => {
  const generated = {
    product_function: [{ id: 'function-accepted', text: '为户外活动提供遮阳。', source_ids: ['e1'] }],
  };
  for (const decision of ['accepted', 'ignored']) {
    let modules = decision === 'accepted'
      ? applyGeneratedAnalysis(createDefaultOverviewModules(), generated)
      : createDefaultOverviewModules();
    const custom = createOverviewModule(`自定义-${decision}`, `custom-${decision}`);
    modules = [...modules, custom];
    modules = addOverviewItem(modules, custom.id, { id: `item-${decision}`, text: '先补充一条内容' });
    modules = updateOverviewItem(modules, custom.id, `item-${decision}`, { text: '人工修订后的内容' });
    modules = removeOverviewItem(modules, custom.id, `item-${decision}`);
    modules = removeOverviewModule(modules, custom.id);
    assert.equal(modules.some((module) => module.id === custom.id), false);
    assert.ok(modules.some((module) => module.field_key === 'product_function'));
    assert.match(archivePage, /decideOverview\('accepted'\)/);
    assert.match(archivePage, /decideOverview\('ignored'\)/);
    const marketEditor = readFileSync(new URL('../src/MarketAnalysisEditor.jsx', import.meta.url), 'utf8');
    assert.match(marketEditor, /addOverviewItem/);
    assert.match(marketEditor, /removeOverviewItem/);
    assert.match(marketEditor, /removeOverviewModule/);
  }
});

test('pending ready suggestions remain actionable after refresh until the user decides', () => {
  requireUiContract(/sessionStorage\.setItem\(`pa-overview-run:/, 'persist pending run ID');
  requireUiContract(/sessionStorage\.getItem\(`pa-overview-run:/, 'restore pending run ID');
  requireUiContract(/sessionStorage\.removeItem\(`pa-overview-run:/, 'clear run ID after decision');
  requireUiContract(/overview_suggestions/, 'recover persisted suggestions');
  requireUiContract(/pendingAnalysis/, 'render pending decision state');
  // Clearing the recovery key as soon as polling sees ready loses the draft
  // after a page refresh, before accept/ignore has been submitted.
  const readyPollingBlock = archivePage.match(/if\(value\.status==='ready'\)([\s\S]*?)if\(value\.status==='failed'\)/)?.[1] || '';
  assert.match(readyPollingBlock, /return}/, 'ready draft polling should stop after loading its preview');
  assert.equal(readyPollingBlock.includes('sessionStorage.removeItem'), false, 'ready draft key must remain until user decision');
});

test('missing price and usage data are shown as unknown rather than a zero-dollar cost', () => {
  requireUiContract(/estimated_cost|actual_cost|费用状态|费用未知/, 'cost estimate/actual cost state');
  requireUiContract(/\.quality===|usage_quality|usageQuality/, 'measured/estimated/unknown usage state');
  requireUiContract(/(?:费用|成本)[^\n]{0,30}未知/, 'unknown amount label');
  rejectUiContract(/\$0(?:\.00)?/, 'render unknown cost as zero dollars');
});

test('research setup defaults to the archive category, explains dates and costs, and remains usable with no category', () => {
  requireUiContract(/pa-research-setup pa-research-scope/, 'keyboard-operable expandable research scope');
  requireUiContract(/categoryContext\|\|archive\?\.category/, 'archive category is the default research scope');
  requireUiContract(/档案数据月份/, 'read-only archive data month');
  requireUiContract(/本次所选资料覆盖范围/, 'read-only selected source coverage');
  requireUiContract(/资料未提供覆盖范围/, 'unknown source coverage is not inferred from production date');
  requireUiContract(/产出时间 \{sourceProductionDate\(source\)\}/, 'source production date is shown separately');
  requireUiContract(/档案月份不用于筛选资料或判断季节性/, 'month is not a research filter');
  requireUiContract(/开始后会调用当前模型并进行公开检索，可能产生模型或检索费用/, 'model and public search cost disclosure');
  requireUiContract(/请先展开研究范围并填写品类/, 'empty category validation');
  assert.ok(archivePage.includes("disabled={busy||overviewRun?.status==='running'||!(categoryContext.trim()||archive.category||'').trim()}"), 'start is blocked while required category is missing');
});

test('running research hides setup while keeping polite progress and the selected source snapshot visible', () => {
  requireUiContract(/ma-generate-disclosure is-running/, 'running state styling');
  requireUiContract(/overview-progress-stage" role="status"/, 'polite live progress stage');
  requireUiContract(/overviewRun\?\.status==='running'&&<AnalysisSourceSummary/, 'running source snapshot');
  const styles = readFileSync(new URL('../src/productArchive.css', import.meta.url), 'utf8');
  assert.match(styles, /\.ma-generate-disclosure\.is-running \.pa-overview-source-list/, 'setup fields collapse during research');
});

test('only a current v3 review opens ahead of the saved report and renders one shared chart', () => {
  requireUiContract(/className="ma-pending" open=\{currentPendingAnalysis\}/, 'current pending report opens by default');
  requireUiContract(/className="ma-saved-analysis" open=\{!currentPendingAnalysis\}/, 'saved report collapses only for a newer review');
  requireUiContract(/seasonalityChart=\{!\(currentPendingAnalysis&&draftMarketAnalysis\?\.schema_version===3\)/, 'only one shared v3 seasonality chart renders');
  const styles = readFileSync(new URL('../src/productArchive.css', import.meta.url), 'utf8');
  assert.match(styles, /\.pa-analysis-decision\{position:static/);
  assert.doesNotMatch(styles, /\.pa-analysis-decision\{position:sticky/);
});
