export const emptyMarketAnalysis = () => ({
  schema_version: 2,
  overview: { text: '', classification: '', basis: '', basis_note: '', evidence_refs: [], limits: '' },
  product_forms: [],
  demand_contexts: [],
  cross_cutting: [],
  needs_validation: [],
});

const id = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`}`;
export const addMarketItem = (analysis, collection, defaults = {}) => ({
  ...analysis,
  [collection]: [...(analysis?.[collection] || []), { id: id(collection.slice(0, 3)), ...newItemDefaults(collection), ...defaults }],
});

export function newItemDefaults(collection) {
  if (['product_forms', 'demand_contexts', 'cross_cutting'].includes(collection)) {
    return {
      classification: 'hypothesis',
      basis: 'inference',
      basis_note: '新建内容尚待资料核实。',
      evidence_refs: [],
    };
  }
  return {};
}
export const updateMarketItem = (analysis, collection, itemId, patch) => ({
  ...analysis,
  [collection]: (analysis?.[collection] || []).map(item => item.id === itemId ? { ...item, ...patch } : item),
});
export const removeMarketItem = (analysis, collection, itemId) => ({
  ...analysis,
  [collection]: (analysis?.[collection] || []).filter(item => item.id !== itemId),
});

export function unlinkMarketItem(analysis, collection, itemId) {
  const next = { ...analysis };
  if (collection === 'product_forms') {
    next.demand_contexts = (analysis.demand_contexts || []).map(item => ({ ...item, product_form_ids: (item.product_form_ids || []).filter(id => id !== itemId) }));
    next.cross_cutting = (analysis.cross_cutting || []).map(item => ({ ...item, product_form_ids: (item.product_form_ids || []).filter(id => id !== itemId) }));
    next.needs_validation = (analysis.needs_validation || []).map(item => ({ ...item, product_form_ids: (item.product_form_ids || []).filter(id => id !== itemId) }));
  }
  if (collection === 'demand_contexts') {
    next.cross_cutting = (analysis.cross_cutting || []).map(item => ({ ...item, context_ids: (item.context_ids || []).filter(id => id !== itemId) }));
    next.needs_validation = (analysis.needs_validation || []).map(item => ({ ...item, context_ids: (item.context_ids || []).filter(id => id !== itemId) }));
  }
  return next;
}

export function contextReadingGroups(context = {}) {
  const definitions = [
    { title: '人群、触发与任务', fields: [['audience', '人群'], ['trigger', '触发'], ['task', '任务']] },
    { title: '阻碍与动机', fields: [['obstacles', '阻碍'], ['motivation', '动机']] },
    { title: '期望结果与商品回应', fields: [['desired_outcome', '期望结果'], ['product_response', '商品回应']] },
    { title: '条件与边界', fields: [['conditions', '条件'], ['boundary', '边界'], ['limits', '限制']] },
  ];
  return definitions.map(group => ({
    title: group.title,
    items: group.fields.map(([key, label]) => ({ key, label, value: String(context[key] || '').trim() })).filter(item => item.value),
  })).filter(group => group.items.length);
}

// Old records stay in their original shape. This is a display grouping only,
// never a conversion into guessed product forms or demand contexts.
export function legacyGroups(modules = []) {
  const groups = [
    ['产品描述与功能', ['product_name', 'product_function']],
    ['人群与使用情境', ['target_audience', 'usage_scenarios', 'needs_motivations', 'buying_motives']],
    ['环境与时间条件', ['cultural_context', 'seasonality']],
    ['其他历史模块', []],
  ];
  const known = new Set(groups.flatMap(([, keys]) => keys));
  return groups.map(([title, keys]) => ({ title, modules: modules.filter(module => keys.includes(module.field_key) || (!keys.length && !known.has(module.field_key))) })).filter(group => group.modules.length);
}
