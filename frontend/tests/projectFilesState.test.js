import test from 'node:test';
import assert from 'node:assert/strict';
import { artifactSourceIdentity, artifactTaskHref, filterProjectArtifacts, formatArtifactDate, formatFileSize, latestProjectArtifacts, normalizeProjectLibrary, PROJECT_FOLDERS } from '../src/projectFilesState.js';

test('project library exposes all six fixed artifact folders, including empty folders', () => {
  const folders = normalizeProjectLibrary({ folders: [{ kind: 'source', count: 2, items: [{ id: 'a' }, { id: 'b' }] }, { kind: 'voc', count: 0, items: [] }, { kind: 'reports', count: 7, items: [{ id: 'hidden' }] }] });
  assert.deepEqual(folders.map(folder => folder.kind), ['source', 'data_x', 'reviews', 'voc', 'keywords', 'other']);
  assert.equal(folders.find(folder => folder.kind === 'data_x').count, 0);
  assert.equal(folders.find(folder => folder.kind === 'source').items.length, 2);
  assert.equal(folders.some(folder => folder.kind === 'reports'), false);
  assert.equal(PROJECT_FOLDERS.length, 6);
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

test('project files keep source versions together and deep-link to keyword tasks', () => {
  const first = { id: 'old', source_type: 'keyword_research', source_id: 'run 1' };
  const second = { id: 'new', source_identity: 'keyword_research:run 1' };
  assert.equal(artifactSourceIdentity(first), 'keyword_research:run 1');
  assert.equal(artifactSourceIdentity(second), 'keyword_research:run 1');
  assert.equal(artifactTaskHref(first), '#/keyword/run/run%201');
  assert.equal(artifactTaskHref({ source_task_url: '#/keyword/run/exact' }), '#/keyword/run/exact');
});

test('project sidebar limits its recent files to twenty newest artifacts', () => {
  const items = Array.from({ length: 25 }, (_, index) => ({ id: String(index), created_at: `2026-01-${String(index + 1).padStart(2, '0')}` }));
  assert.equal(latestProjectArtifacts(items).length, 20);
  assert.equal(latestProjectArtifacts(items)[0].id, '24');
  assert.equal(latestProjectArtifacts(items).at(-1).id, '5');
});
