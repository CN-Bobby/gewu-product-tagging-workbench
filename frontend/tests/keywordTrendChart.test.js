import test from 'node:test';
import assert from 'node:assert/strict';
import { formatSearches, trendRows } from '../src/keywordTrendChart.js';

test('relative mode compares each keyword with its own available-month average', () => {
  const report = { months: ['2025-01', '2025-02', '2025-03'], keywords: [
    { keyword: 'work pants', values: [100, 200, null] },
    { keyword: 'linen pants', values: [10, 30, 50] },
  ] };
  const rows = trendRows(report);
  assert.deepEqual(rows.map(row => row.k0), [67, 133, null]);
  assert.deepEqual(rows.map(row => row.k1), [33, 100, 167]);
  assert.deepEqual(rows.map(row => row.raw0), [100, 200, null]);
  assert.equal(trendRows(report, 'raw')[1].k1, 30);
  assert.equal(formatSearches(null), '—');
});
