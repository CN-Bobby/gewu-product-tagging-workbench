import test from 'node:test';
import assert from 'node:assert/strict';
import { addMarketItem, contextReadingGroups, emptyMarketAnalysis, legacyGroups, newItemDefaults, removeMarketItem, unlinkMarketItem, updateMarketItem } from '../src/marketAnalysis.js';

test('new market analysis uses the v2 sections without invented content', () => {
  assert.deepEqual(emptyMarketAnalysis(), {
    schema_version: 2,
    overview: { text: '', classification: '', basis: '', basis_note: '', evidence_refs: [], limits: '' },
    product_forms: [], demand_contexts: [], cross_cutting: [], needs_validation: [],
  });
});

test('legacy records remain grouped by their original segment and keep ungrouped modules separate', () => {
  const modules = [
    { id: 'audience', field_key: 'target_audience', title: '目标人群', items: [{ id: 'a', segment_id: 'S1', segment_name: 'Cargo', text: '人群内容' }] },
    { id: 'function', field_key: 'product_function', title: '产品与功能', items: [{ id: 'b', segment_id: 'S1', segment_name: 'Cargo', text: '功能内容' }, { id: 'c', text: '人工内容' }] },
  ];
  const groups = legacyGroups(modules);
  assert.deepEqual(groups.map(group => [group.title, group.modules.map(module => module.id)]), [
    ['产品描述与功能', ['function']], ['人群与使用情境', ['audience']],
  ]);
  assert.equal(modules.flatMap(module => module.items).filter(item => item.segment_id === 'S1').length, 2);
  assert.equal(modules.flatMap(module => module.items).filter(item => !item.segment_id)[0].text, '人工内容');
});

test('legacy grouping includes the current buying_motives key and keeps its older alias', () => {
  const modules = [
    { id: 'buying', field_key: 'buying_motives', title: '需求与动机' },
    { id: 'old-motive', field_key: 'needs_motivations', title: '旧需求与动机' },
  ];
  const group = legacyGroups(modules).find(item => item.title === '人群与使用情境');
  assert.deepEqual(group.modules.map(module => module.id), ['buying', 'old-motive']);
});

test('market-analysis collection edits preserve unrelated contexts and cross-cutting associations', () => {
  let value = emptyMarketAnalysis();
  value = addMarketItem(value, 'product_forms', { name: 'Cargo' });
  const formId = value.product_forms[0].id;
  value = addMarketItem(value, 'demand_contexts', { name: '工地班次', product_form_ids: [formId] });
  value = addMarketItem(value, 'cross_cutting', { title: '尺码适配', product_form_ids: [formId] });
  const contextId = value.demand_contexts[0].id;
  value = updateMarketItem(value, 'demand_contexts', contextId, { product_form_ids: [] });
  value = removeMarketItem(value, 'product_forms', formId);
  assert.equal(value.demand_contexts[0].name, '工地班次');
  assert.deepEqual(value.demand_contexts[0].product_form_ids, []);
  assert.equal(value.cross_cutting[0].title, '尺码适配');
});

test('read-only demand contexts group populated fields and omit empty fields and groups', () => {
  const groups = contextReadingGroups({
    audience: '夜班施工人员', trigger: '夜间照明不足', task: '确认接线',
    obstacles: '手部不便', motivation: '减少返工', desired_outcome: '准确完成',
    product_response: '提供可调光灯', conditions: '', boundary: '仅室内', limits: '不适用于水下',
    basis_note: '来自访谈和商品资料', classification: 'hypothesis', evidence_refs: ['src-a'],
  });
  assert.deepEqual(groups.map(group => group.title), [
    '人群、触发与任务', '阻碍与动机', '期望结果与商品回应', '条件与边界',
  ]);
  assert.deepEqual(groups[0].items.map(item => item.label), ['人群', '触发', '任务']);
  assert.deepEqual(groups[3].items.map(item => item.label), ['边界', '限制']);
  assert.equal(groups.flatMap(group => group.items).some(item => item.key === 'conditions'), false);
  assert.equal(contextReadingGroups({}).length, 0);
});

test('new editable form, context and cross-cutting items include backend-required defaults', () => {
  for (const collection of ['product_forms', 'demand_contexts', 'cross_cutting']) {
    assert.deepEqual(newItemDefaults(collection), {
      classification: 'hypothesis',
      basis: 'inference',
      basis_note: '新建内容尚待资料核实。',
      evidence_refs: [],
    });
    const item = addMarketItem(emptyMarketAnalysis(), collection)[collection][0];
    assert.equal(item.classification, 'hypothesis');
    assert.equal(item.basis, 'inference');
    assert.deepEqual(item.evidence_refs, []);
  }
});

test('deleting forms and contexts removes dangling validation links but preserves validation text', () => {
  const formId = 'form-1';
  const contextId = 'context-1';
  const analysis = {
    ...emptyMarketAnalysis(),
    product_forms: [{ id: formId, name: 'Cargo' }],
    demand_contexts: [{ id: contextId, name: '夜班', product_form_ids: [formId] }],
    cross_cutting: [{ id: 'cross-1', product_form_ids: [formId], context_ids: [contextId] }],
    needs_validation: [{ id: 'validate-1', text: '确认场景适用性', product_form_ids: [formId], context_ids: [contextId] }],
  };

  let next = unlinkMarketItem(analysis, 'product_forms', formId);
  assert.deepEqual(next.needs_validation[0].product_form_ids, []);
  assert.deepEqual(next.needs_validation[0].context_ids, [contextId]);
  assert.equal(next.needs_validation[0].text, '确认场景适用性');
  assert.deepEqual(next.demand_contexts[0].product_form_ids, []);
  assert.deepEqual(next.cross_cutting[0].product_form_ids, []);

  next = unlinkMarketItem(analysis, 'demand_contexts', contextId);
  assert.deepEqual(next.needs_validation[0].context_ids, []);
  assert.deepEqual(next.needs_validation[0].product_form_ids, [formId]);
  assert.equal(next.needs_validation[0].text, '确认场景适用性');
  assert.deepEqual(next.cross_cutting[0].context_ids, []);
});
