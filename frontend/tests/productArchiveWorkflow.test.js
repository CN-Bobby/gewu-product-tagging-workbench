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
  assert.match(projectPage, /artifactDownloadUrl\(workspaceId, item\)/);
  assert.match(projectFilesState, /artifacts\/\$\{encodeURIComponent\(item\.id\)\}\/download/);
  assert.match(projectPage, /item\.downloadable/);
  assert.match(projectPage, /pm-sort/);
  assert.match(projectPage, /projects\/\$\{workspaceId\}\/folders/);
  assert.doesNotMatch(projectPage, /project\.root/);
});

test('global project tree is present on application pages and previews use workbook and sandboxed HTML contracts', () => {
  assert.match(taggingPage, /<ProjectSidebar/);
  assert.match(projectTree, /\/api\/projects\/\$\{encodeURIComponent\(project\.id\)\}\/library/);
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

test('overview suggestions are explicitly generated, polled and only use local evidence after user choice', () => {
  assert.match(archivePage, /\/overview-drafts/);
  assert.match(archivePage, /local_only:localOnly/);
  assert.match(archivePage, /仅本地资料继续/);
  assert.match(archivePage, /web_unavailable/);
  assert.match(archivePage, /正在生成档案建议/);
  assert.match(archivePage, /setTimeout/);
});

test('overview suggestion evidence and per-field accept or ignore controls are visible', () => {
  assert.match(archivePage, /overview_suggestions/);
  assert.match(archivePage, /source_ids/);
  assert.match(archivePage, /待验证/);
  assert.match(archivePage, /采纳建议/);
  assert.match(archivePage, /忽略建议/);
  assert.match(archivePage, /source_type/);
});

test('overview save protects manual edits and sends accepted suggestion identities with an update precondition', () => {
  assert.match(archivePage, /accepted_suggestion_ids/);
  assert.match(archivePage, /expected_updated_at/);
  const helperSource = archivePage.match(/function getAcceptedSuggestionIds[^\n]+/);
  assert.ok(helperSource, 'accepted suggestion identity filtering helper exists');
  const getAcceptedSuggestionIds = new Function(`${helperSource[0]}; return getAcceptedSuggestionIds`)();
  const suggestions = { product_name: [{ id: 's-1', text: 'market opportunity' }] };
  assert.deepEqual(getAcceptedSuggestionIds({ product_name: 's-1' }, suggestions, { product_name: 'market opportunity' }), ['s-1']);
  assert.deepEqual(getAcceptedSuggestionIds({ product_name: 's-1' }, suggestions, { product_name: 'manual rewrite' }), []);
  assert.match(archivePage, /冲突/);
});

test('structured suggestion scope is formatted instead of rendered as a React object', () => {
  assert.match(archivePage, /function formatSuggestionScope/);
  assert.match(archivePage, /scope\.category/);
  assert.match(archivePage, /scope\.category_context/);
  assert.match(archivePage, /scope\.data_month_context/);
  assert.match(archivePage, /scope\.coverage/);
  assert.doesNotMatch(archivePage, /\$\{suggestion\.scope\}/);
});

test('ignored suggestion identities are sent as an array and include only locally ignored items', () => {
  assert.match(archivePage, /ignored_suggestion_ids/);
  const helperSource = archivePage.match(/function getIgnoredSuggestionIds[^\n]+/);
  assert.ok(helperSource, 'ignored suggestion identity filtering helper exists');
  const getIgnoredSuggestionIds = new Function(`${helperSource[0]}; return getIgnoredSuggestionIds`)();
  assert.deepEqual(getIgnoredSuggestionIds({ 's-1': true, 's-2': false, 's-3': true }), ['s-1', 's-3']);
});

test('optional public source URLs are separate from overview fields and sent as a trimmed list', () => {
  assert.match(archivePage, /pa-source-url-input/);
  assert.match(archivePage, /source_urls:localOnly\?\[\]:parseSourceUrls\(sourceUrls\)/);
  assert.match(archivePage, /可选网页来源/);
  const helperSource = archivePage.match(/function parseSourceUrls[^\n]+/);
  assert.ok(helperSource, 'source URL parsing helper exists');
  const parseSourceUrls = new Function(`${helperSource[0]}; return parseSourceUrls`)();
  assert.deepEqual(parseSourceUrls(' https://one.example/a \n\nhttps://two.example/b '), ['https://one.example/a', 'https://two.example/b']);
  assert.throws(() => parseSourceUrls('https://1.example\nhttps://2.example\nhttps://3.example\nhttps://4.example'), /最多填写 3 个/);
});

test('empty unsupported overview fields remain marked for validation without replacing saved text', () => {
  assert.match(archivePage, /pa-validation-marker/);
  const helperSource = archivePage.match(/function needsOverviewValidation[^\n]+/);
  assert.ok(helperSource, 'overview evidence validation helper exists');
  const needsOverviewValidation = new Function(`${helperSource[0]}; return needsOverviewValidation`)();
  assert.equal(needsOverviewValidation('', [], [], 'seasonality'), true);
  assert.equal(needsOverviewValidation('', [{ text: 'May to July', needs_validation: false }], [], 'seasonality'), false);
  assert.equal(needsOverviewValidation('', [{ text: '', needs_validation: true }], { seasonality: true }, 'seasonality'), true);
  assert.equal(needsOverviewValidation('historical seasonality text', [], { seasonality: true }, 'seasonality'), false);
});

test('successful overview save retires local task suggestions without clearing saved field text', () => {
  assert.match(archivePage, /setOverviewRun\(null\);setOverviewRunId\(''\);sessionStorage\.removeItem/);
  assert.doesNotMatch(archivePage, /setArchive\(value\);setOverviewRun\(null\);setOverviewRunId\(''\);sessionStorage\.removeItem\([^;]+\);setOverview\(/);
});

test('overview generation asks for missing category and month instead of inferring a filename', () => {
  assert.match(archivePage, /category_context/);
  assert.match(archivePage, /data_month_context/);
  assert.match(archivePage, /填写测试主题/);
  assert.match(archivePage, /输入月份/);
  assert.doesNotMatch(archivePage, /source_filename.*split/);
});
