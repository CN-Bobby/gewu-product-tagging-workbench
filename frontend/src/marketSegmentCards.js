export const SEGMENT_FIELDS = [
  ['product', '产品是什么'], ['audience', '谁使用'], ['scenarios', '使用情境'],
  ['culture', '文化背景'], ['seasonality', '季节性'],
];

// Only move an explicit trailing sample-boundary sentence in the reading view.
// The stored overview and the editor retain the original, unmodified text.
export function overviewReadingParts(value) {
  const text = String(value || '').trim();
  const marker = /样本边界[：:]/g;
  const matches = [...text.matchAll(marker)];
  const last = matches.at(-1);
  if (!last || last.index < text.length / 2) return { summary: text, boundary: '' };
  return {
    summary: text.slice(0, last.index).trim(),
    boundary: text.slice(last.index + last[0].length).trim(),
  };
}

// Legacy reports keep their exact saved wording. Break only at existing
// punctuation to make long paragraphs easier to scan in the reading view.
export function overviewReadingParagraphs(value, targetLength = 45) {
  const text = String(value || '').trim();
  if (!text) return [];
  const clauses = text.match(/[^，,；;。]+[，,；;。]?/g) || [text];
  const paragraphs = [];
  let current = '';
  for (const clause of clauses) {
    if (current && current.length >= targetLength) {
      paragraphs.push(current);
      current = '';
    }
    current += clause;
  }
  if (current) paragraphs.push(current);
  if (paragraphs.length > 1 && paragraphs.at(-1).length < 30) {
    paragraphs[paragraphs.length - 2] += paragraphs.pop();
  }
  return paragraphs;
}

const uid = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`}`;

export const emptySegment = () => ({
  id: uid('S'), name: '',
  fields: Object.fromEntries(SEGMENT_FIELDS.map(([key]) => [key, { items: [], missing_reason: '待补充。' }])),
  needs_validation: [],
});

export const newSegmentClaim = (field, text = '') => ({
  text, classification: 'hypothesis', basis: 'inference',
  basis_note: '人工新增内容，待核实。', evidence_refs: [], limits: '',
  ...(field === 'seasonality' ? { kind: 'possible_use_period', measured_months: [] } : {}),
});

export const editSegmentClaim = (claim, text) => ({
  ...claim, text, classification: 'hypothesis', basis: 'inference',
  basis_note: '人工修改，原依据需重新核对。', evidence_refs: [],
  ...(['sales_peak', 'search_demand_peak'].includes(claim.kind) ? { kind: 'possible_use_period', measured_months: [] } : {}),
});

export function updateSegment(analysis, id, updater) {
  return { ...analysis, segments: (analysis.segments || []).map(segment => segment.id === id ? updater(segment) : segment) };
}

export function moveSegment(analysis, id, direction) {
  const segments = [...(analysis.segments || [])];
  const index = segments.findIndex(segment => segment.id === id);
  const next = index + direction;
  if (index < 0 || next < 0 || next >= segments.length) return analysis;
  [segments[index], segments[next]] = [segments[next], segments[index]];
  return { ...analysis, segments };
}
