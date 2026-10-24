const DEFAULT_MODULES = [
  ['product_name', '产品概况'],
  ['product_function', '产品与功能'],
  ['target_audience', '目标人群'],
  ['usage_scenarios', '使用场景'],
  ['buying_motives', '需求与动机'],
  ['cultural_context', '文化与环境'],
  ['seasonality', '季节性'],
];

const makeId = (prefix = 'overview') => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`}`;

// A draft from before the saved analysis remains accessible, but must not
// displace the newer accepted report when the archive is opened again.
export function isCurrentPendingRun(run, savedRunId, savedRun) {
  if (!run || !['ready', 'pending_review'].includes(run.status) || run.review_decision) return false;
  if (!savedRunId) return true;
  if (run.id === savedRunId) return false;
  const pendingAt = Date.parse(run.created_at || '');
  const savedAt = Date.parse(savedRun?.created_at || '');
  // Only suppress a draft when both timestamps prove it predates the saved report.
  return !Number.isFinite(pendingAt) || !Number.isFinite(savedAt) || pendingAt > savedAt;
}

function asItems(value, fieldKey) {
  if (Array.isArray(value)) return value.filter((item) => item && typeof item === 'object' && String(item.text || '').trim());
  if (!String(value || '').trim()) return [];
  return [{ id: makeId(fieldKey), text: String(value), source_type: 'legacy_overview', source_ids: [] }];
}

export function workflowKeywordTerms(archive) {
  const integration = archive?.integrations?.keyword_research;
  const completedRuns = new Set((integration?.sources || [])
    .filter((source) => source && typeof source === 'object' && Number(source.keyword_count) > 0 && source.run_id)
    .map((source) => String(source.run_id)));
  const terms = integration?.representative_terms;
  if (!Array.isArray(terms)) return [];
  const seen = new Set();
  return terms.filter((term) => {
    if (!term || typeof term !== 'object' || !String(term.keyword || '').trim()) return false;
    if (!term.source_run_id || !completedRuns.has(String(term.source_run_id))) return false;
    const normalized = String(term.keyword).trim().toLocaleLowerCase();
    if (seen.has(normalized)) return false;
    seen.add(normalized);
    return true;
  });
}

export function createDefaultOverviewModules(archive = {}) {
  const persisted = Array.isArray(archive.overview_modules) ? archive.overview_modules : null;
  if (persisted && (persisted.length > 0 || archive.overview_modules_initialized === true)) {
    const modules = persisted.filter((module) => module.field_key !== 'workflow_keywords').map((module) => ({
      ...module,
      items: Array.isArray(module.items) ? module.items.map((item) => ({ ...item })) : [],
    }));
    const terms = workflowKeywordTerms(archive);
    if (terms.length) modules.push({
      id: 'module-workflow-keywords', field_key: 'workflow_keywords', title: '业务关键词', read_only: true,
      items: terms.map((term, index) => ({
        id: `keyword-${term.source_run_id}-${index}`, text: String(term.keyword).trim(),
        source_type: 'keyword_research', source_ids: [term.source_run_id], links: [],
      })),
    });
    return modules;
  }

  const overview = archive.overview || {};
  const modules = DEFAULT_MODULES.map(([field_key, title]) => ({
    id: `module-${field_key}`,
    field_key,
    title,
    items: asItems(overview[field_key], field_key),
  }));
  const keywordTerms = workflowKeywordTerms(archive);
  if (keywordTerms.length) modules.push({
    id: 'module-workflow-keywords',
    field_key: 'workflow_keywords',
    title: '业务关键词',
    read_only: true,
    items: keywordTerms.map((term, index) => ({
      id: `keyword-${term.source_run_id || term.task_id || index}-${index}`,
      text: String(term.keyword).trim(),
      source_type: 'keyword_research',
      source_ids: [term.source_run_id || term.task_id].filter(Boolean),
      links: [],
    })),
  });
  return modules;
}

export function applyGeneratedAnalysis(currentModules, suggestions = {}) {
  const base = currentModules?.length ? currentModules : createDefaultOverviewModules();
  const modules = base.map((module) => ({
    ...module,
    items: Array.isArray(module.items) ? [...module.items] : [],
  }));
  for (const [fieldKey, entries] of Object.entries(suggestions || {})) {
    if (fieldKey === 'representative_keywords' || fieldKey === 'workflow_keywords' || !Array.isArray(entries)) continue;
    const module = modules.find((item) => item.field_key === fieldKey);
    if (!module || module.read_only) continue;
    const known = new Set(module.items.map((item) => item.id).filter(Boolean));
    for (const entry of entries) {
      const text = String(entry?.text || '').trim();
      if (!text || (entry.id && known.has(entry.id))) continue;
      module.items.push({
        id: entry.id || makeId(fieldKey),
        text,
        source_type: entry.source_type || 'overview_draft',
        source_ids: Array.isArray(entry.source_ids) ? [...entry.source_ids] : [],
        links: Array.isArray(entry.links) ? [...entry.links] : [],
        evidence_refs: Array.isArray(entry.evidence_refs) ? [...entry.evidence_refs] : [],
        segment_id: entry.segment_id || null,
        segment_name: entry.segment_name || null,
        segment_classification: entry.segment_classification === 'supported' ? 'supported'
          : entry.segment_classification === 'hypothesis' ? 'hypothesis' : null,
        basis: entry.basis || 'observation',
        basis_note: entry.basis_note || '',
        limits: entry.limits || '',
      });
      if (entry.id) known.add(entry.id);
    }
  }
  return modules;
}

export function createOverviewModule(title = '新模块', id = makeId('module')) {
  return { id, field_key: null, title: String(title || '').trim() || '新模块', items: [] };
}

export function removeOverviewModule(modules, id) {
  return (modules || []).filter((module) => module.id !== id);
}

export function addOverviewItem(modules, moduleId, item = {}) {
  return (modules || []).map((module) => module.id === moduleId
    ? { ...module, items: [...(module.items || []), { id: item.id || makeId('item'), text: item.text || '', source_type: 'manual', source_ids: [], links: [] }] }
    : module);
}

export function updateOverviewModule(modules, moduleId, update) {
  return (modules || []).map((module) => module.id === moduleId ? { ...module, ...update } : module);
}

export function updateOverviewItem(modules, moduleId, itemId, update) {
  return (modules || []).map((module) => module.id !== moduleId ? module : {
    ...module,
    items: (module.items || []).map((item) => item.id === itemId ? { ...item, ...update } : item),
  });
}

export function removeOverviewItem(modules, moduleId, itemId) {
  return (modules || []).map((module) => module.id !== moduleId ? module : {
    ...module,
    items: (module.items || []).filter((item) => item.id !== itemId),
  });
}

export function moveOverviewModule(modules, moduleId, direction) {
  const result = [...(modules || [])];
  const index = result.findIndex((module) => module.id === moduleId);
  const next = index + direction;
  if (index < 0 || next < 0 || next >= result.length) return result;
  [result[index], result[next]] = [result[next], result[index]];
  return result;
}
