import test from 'node:test';
import assert from 'node:assert/strict';
import { editSegmentClaim, emptySegment, moveSegment, newSegmentClaim, overviewReadingParagraphs, overviewReadingParts, SEGMENT_FIELDS, updateSegment } from '../src/marketSegmentCards.js';

test('overview reading layout moves only a trailing sample boundary without changing the saved text', () => {
  const text = '主体形态是防晒泳衣上衣。价格与样本分布仍需核对。样本边界：仅覆盖一个节点。';
  assert.deepEqual(overviewReadingParts(text), { summary: '主体形态是防晒泳衣上衣。价格与样本分布仍需核对。', boundary: '仅覆盖一个节点。' });
  assert.deepEqual(overviewReadingParts('样本边界：仅覆盖一个节点。主体形态仍需核对。'), { summary: '样本边界：仅覆盖一个节点。主体形态仍需核对。', boundary: '' });
});

test('legacy overview wraps existing clauses for reading without losing or changing wording', () => {
  const text = '本样本为 US 站男士休闲裤，商品构型包含工装裤与卡其裤；价格集中在一个区间，评论量差异明显，履约方式有多种，样本中既有在售多年的款式，也有最近上架的商品。';
  const paragraphs = overviewReadingParagraphs(text, 20);
  assert.ok(paragraphs.length > 1);
  assert.equal(paragraphs.join(''), text);
  const withShortEnding = overviewReadingParagraphs('样本来自 US 站点 100 条商品，品类集中于 Casual 长裤，产品形态包括弹力斜纹裤、工装裤和运动裤，价格覆盖多个区间，评论量差异很大，呈现新旧商品并存。', 30);
  assert.ok(withShortEnding.at(-1).length >= 30);
});

test('new card has five independently editable fields', () => {
  const segment = emptySegment();
  assert.deepEqual(Object.keys(segment.fields), SEGMENT_FIELDS.map(([key]) => key));
  assert.ok(Object.values(segment.fields).every(field => field.items.length === 0 && field.missing_reason));
  assert.equal(newSegmentClaim('seasonality').kind, 'possible_use_period');
});

test('editing a conclusion clears stale evidence and measured peak status', () => {
  const next = editSegmentClaim({ text: 'old', basis: 'observation', classification: 'supported',
    evidence_refs: ['old-source'], kind: 'sales_peak', measured_months: ['2026-01', '2026-02', '2026-03'] }, '新表述');
  assert.equal(next.text, '新表述');
  assert.deepEqual(next.evidence_refs, []);
  assert.equal(next.basis, 'inference');
  assert.equal(next.kind, 'possible_use_period');
});

test('reordering cards keeps identifiers and leaves the other card content intact', () => {
  const first = { ...emptySegment(), name: '作业裤' };
  const second = { ...emptySegment(), name: '休闲裤' };
  const value = { schema_version: 3, segments: [first, second] };
  const edited = updateSegment(value, first.id, item => ({ ...item, name: '工装裤' }));
  assert.equal(edited.segments[1].name, '休闲裤');
  const moved = moveSegment(edited, first.id, 1);
  assert.deepEqual(moved.segments.map(item => item.name), ['休闲裤', '工装裤']);
  assert.equal(value.segments[0].name, '作业裤');
});
