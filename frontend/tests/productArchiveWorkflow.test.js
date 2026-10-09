import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const taggingPage = readFileSync(new URL('../src/main.jsx', import.meta.url), 'utf8');
const archivePage = readFileSync(new URL('../src/productArchive.jsx', import.meta.url), 'utf8');
const projectPage = readFileSync(new URL('../src/projectManager.jsx', import.meta.url), 'utf8');
const projectTree = readFileSync(new URL('../src/projectSidebar.jsx', import.meta.url), 'utf8');
const projectPreview = readFileSync(new URL('../src/projectPreview.jsx', import.meta.url), 'utf8');
const projectFilesState = readFileSync(new URL('../src/projectFilesState.js', import.meta.url), 'utf8');

test('tagging page owns guarded project dispatch from the existing selected rows', () => {
  assert.match(taggingPage, /workflow-dispatch-bar/);
  assert.match(taggingPage, /已勾选 \{selected\.length\} 行/);
  assert.match(taggingPage, /ensure-archive/);
  assert.match(taggingPage, /from-product-archive\/\$\{archive\.id\}\/runs/);
  assert.match(taggingPage, /from-product-archive\/\$\{archive\.id\}/);
  assert.match(taggingPage, /只推送商品详情链接中的子 ASIN/);
  assert.match(taggingPage, /any_exported/);
  assert.match(taggingPage, /最多选择 100 行/);
  assert.match(taggingPage, /mapping_confirmed/);
});

test('sample page is read-only and directs unconfirmed workflow back to tagging', () => {
  assert.doesNotMatch(archivePage, /推送关键词调研/);
  assert.doesNotMatch(archivePage, /启动 Amazon 评论采集/);
  assert.match(archivePage, /只读预览/);
  assert.match(archivePage, /返回商品打标/);
});

test('project library uses the artifact API, folder routes, search, time sorting and downloads', () => {
  assert.match(projectPage, /\/api\/projects\/\$\{workspaceId\}\/library/);
  assert.match(projectPage, /filterProjectArtifacts/);
  assert.match(projectPage, /artifacts\/\$\{item\.id\}\/download/);
  assert.match(projectPage, /item\.downloadable/);
  assert.match(projectPage, /pm-sort/);
  assert.match(projectPage, /projects\/\$\{workspaceId\}\/folders/);
  assert.doesNotMatch(projectPage, /project\.root/);
});

test('global project tree is present on application pages and previews use workbook and sandboxed HTML contracts', () => {
  assert.match(taggingPage, /<ProjectSidebar/);
  assert.match(projectTree, /\/api\/projects\/\$\{project\.id\}\/library/);
  assert.match(projectFilesState, /data_x/);
  assert.match(projectPreview, /\/workbook\?/);
  assert.match(projectPreview, /limit: '50'/);
  assert.match(projectPreview, /sandbox=""/);
  assert.match(projectPreview, /\/html/);
});

test('Product Archive confirms a mapping with the backend PUT contract', () => {
  assert.match(archivePage, /\/api\/product-archive\/archives\/\$\{archiveId\}\/mapping`,\{mapping\},'PUT'\)/);
});

test('Product Archive keeps chart, image preview, slicer and project artifact access visible', () => {
  assert.match(archivePage, /role="dialog" aria-modal="true"/);
  assert.match(archivePage, /价格 &amp; 销量 \/ 链接数/);
  assert.match(archivePage, /档案指标已按 Data X 标签重新计算/);
  assert.match(archivePage, /artifacts\/\$\{item\.id\}\/download/);
  assert.match(archivePage, /历史文件，仅登记/);
});
