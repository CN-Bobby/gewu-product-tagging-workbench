import test from 'node:test';
import assert from 'node:assert/strict';
import { filterProjectArtifacts, formatArtifactDate, formatFileSize, normalizeProjectLibrary, PROJECT_FOLDERS } from '../src/projectFilesState.js';

test('project library exposes only the four fixed artifact folders, including empty folders', () => {
  const folders = normalizeProjectLibrary({ folders: [{ kind: 'source', count: 2, items: [{ id: 'a' }, { id: 'b' }] }, { kind: 'voc', count: 0, items: [] }, { kind: 'reports', count: 7, items: [{ id: 'hidden' }] }] });
  assert.deepEqual(folders.map(folder => folder.kind), ['source', 'data_x', 'reviews', 'voc']);
  assert.equal(folders.find(folder => folder.kind === 'data_x').count, 0);
  assert.equal(folders.find(folder => folder.kind === 'source').items.length, 2);
  assert.equal(folders.some(folder => folder.kind === 'reports'), false);
  assert.equal(PROJECT_FOLDERS.length, 4);
});

test('project artifacts search visible metadata and sort newest first', () => {
  const artifacts = [
    { kind: 'source', name: 'source.xlsx', source_type: 'dataset', created_at: '2026-01-01' },
    { kind: 'voc', name: 'voc-report.html', source_type: 'voc_report', created_at: '2026-02-01' },
  ];
  assert.deepEqual(filterProjectArtifacts(artifacts).map(item => item.name), ['voc-report.html', 'source.xlsx']);
  assert.deepEqual(filterProjectArtifacts(artifacts, 'REPORT').map(item => item.name), ['voc-report.html']);
  assert.deepEqual(filterProjectArtifacts(null), []);
});

test('project files show readable sizes and tolerate bad timestamps', () => {
  assert.equal(formatFileSize(1024), '1.0 KB');
  assert.equal(formatFileSize(1024 ** 2), '1.0 MB');
  assert.equal(formatFileSize(-1), '大小未知');
  assert.equal(formatArtifactDate('bad date'), '时间未知');
});
