import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const taggingPage = readFileSync(new URL('../src/main.jsx', import.meta.url), 'utf8');
const archivePage = readFileSync(new URL('../src/productArchive.jsx', import.meta.url), 'utf8');
const marketEditor = readFileSync(new URL('../src/MarketAnalysisEditor.jsx', import.meta.url), 'utf8');
const overviewHelpers = readFileSync(new URL('../src/productArchiveOverview.js', import.meta.url), 'utf8');
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

test('Product Archive keeps chart, image preview and slicer without the full project file list', () => {
  assert.match(archivePage, /role="dialog" aria-modal="true"/);
  assert.match(archivePage, /价格 &amp; 销量 \/ 链接数/);
  assert.match(archivePage, /档案指标已按 Data X 标签重新计算/);
  assert.match(archivePage, /本次使用资料/);
  assert.doesNotMatch(archivePage, /历史文件，仅登记/);
});

test('overview suggestions use the four linked business outputs and poll their result', () => {
  assert.match(archivePage, /\/overview-drafts/);
  assert.match(archivePage, /overview-sources/);
  assert.match(archivePage, /本次分析的业务产物/);
  assert.match(archivePage, /selected_source_ids:selectedOverviewSources/);
  assert.match(archivePage, /档案关联打标表（含图片）/);
  assert.match(archivePage, /关键词任务产出表格/);
  assert.match(archivePage, /评论任务产出表格/);
  assert.match(archivePage, /VOC HTML 报告/);
  assert.match(archivePage, /public_research:true/);
  assert.doesNotMatch(archivePage, /请先选择至少一份当前档案关联的业务产物/);
  assert.doesNotMatch(archivePage, /pa-source-url-input|parseSourceUrls|source_urls:/);
  assert.match(archivePage, /分析阶段/);
  assert.match(archivePage, /setTimeout/);
  assert.match(archivePage, /public_research_diagnostics\?\.message/);
  assert.match(archivePage, /public_research_status==='partial'/);
  assert.match(archivePage, /input_routing\.text_chars/);
});

test('switching archive routes remounts the page to isolate draft state', () => {
  assert.match(taggingPage, /<ProductArchive key=\{route\.archiveId\|\|'archive-list'\}/);
});

test('overview uses one whole-analysis decision and persists editable dynamic modules', () => {
  assert.match(archivePage, /保留整份分析/);
  assert.match(archivePage, /忽略整份分析/);
  assert.doesNotMatch(archivePage, /采纳建议|忽略建议/);
  assert.match(archivePage, /analysisSuggestions/);
  assert.match(archivePage, /overview_modules:editableModules/);
  assert.match(archivePage, /market_analysis:market/);
  assert.match(archivePage, /draftMarketAnalysis/);
  assert.match(marketEditor, /添加形态/);
  assert.match(marketEditor, /添加情境/);
  assert.match(marketEditor, /添加待验证/);
  assert.match(marketEditor, /product_form_ids/);
  assert.match(marketEditor, /context_ids/);
  assert.match(marketEditor, /删除商品形态/);
  assert.match(marketEditor, /删除需求情境/);
  assert.match(marketEditor, /按原有信息保留/);
  assert.match(archivePage, /accepted_suggestion_ids/);
  assert.match(archivePage, /ignored_suggestion_ids/);
  assert.match(archivePage, /expected_updated_at/);
  assert.match(archivePage, /冲突/);
});

test('v2 overall review records ignore without patching saved archive content', () => {
  assert.match(archivePage, /overview-drafts\/\$\{overviewRunId\}\/review/);
  assert.match(archivePage, /\{decision,expected_updated_at:archive\?\.updated_at/);
  assert.match(archivePage, /decision==='accepted'\?\{market_analysis:draftMarketAnalysis\}:\{\}/);
  assert.match(archivePage, /const saved=response\.archive\|\|await api/);
  assert.match(readFileSync(new URL('../src/productArchive.css', import.meta.url), 'utf8'), /details\.ma-old-draft-disclosure:not\(\[open\]\)>.overview-suggestion-preview\{display:none/);
  assert.match(archivePage, /setOverviewRun\(null\);setOverviewRunId\(''\);sessionStorage\.removeItem/);
});

test('archive list render tolerates the archive being unset before route data loads', () => {
  assert.doesNotMatch(archivePage, /overviewRun\?\.suggestions\?\?archive\.overview_suggestions/);
  assert.match(archivePage, /overviewRun\?\.suggestions\?\?archive\?\.overview_suggestions/);
});

test('overview keywords are read only from archived keyword workflow results', () => {
  assert.match(overviewHelpers, /createDefaultOverviewModules/);
  assert.match(overviewHelpers, /title: '业务关键词', read_only: true/);
  assert.match(overviewHelpers, /read_only/);
});

test('analysis evidence metadata and product narrative have distinct presentation', () => {
  assert.match(marketEditor, /证据来源/);
  assert.match(marketEditor, /商品完整描述/);
  assert.match(marketEditor, /商品形态：旧档案未记录/);
  assert.match(marketEditor, /segment_id/);
  assert.match(marketEditor, /sourceManifest = \[\]/);
  assert.match(marketEditor, /sourceManifest=\{sourceManifest\}/);
  assert.doesNotMatch(marketEditor, /\['classification', 'limits'\]\.map/);
  assert.match(marketEditor, /readOnly \? <ContextReadingSummary context=\{context\} \/>/);
  assert.match(marketEditor, /collections\.demand_contexts\.fields\.map/);
  assert.match(marketEditor, /function Basis\(/);
  assert.match(marketEditor, /<Basis readOnly=\{readOnly\} value=\{item\.basis\}/);
  assert.match(marketEditor, /<Basis readOnly=\{readOnly\} value=\{form\.basis\}/);
  assert.match(marketEditor, /readOnly \? <AssociationSummary title="关联商品形态"/);
  assert.match(marketEditor, /emptyLabel="未归类"/);
  assert.match(marketEditor, /readOnly \? <AssociationSummary title="关联商品形态与情境"/);
  assert.match(marketEditor, /emptyLabel="未关联形态或情境"/);
  assert.match(marketEditor, /: <fieldset className="ma-associations"><legend>关联商品形态/);
  assert.match(marketEditor, /!readOnly && <button type="button" onClick=\{\(\) => onChange\(removeMarketItem\(analysis, 'cross_cutting'/);
  assert.match(archivePage, /查看分析覆盖与过滤说明/);
  assert.match(archivePage, /applyGeneratedAnalysis/);
});

test('legacy editor restores custom module and item add/remove actions with read-only protection', () => {
  assert.match(marketEditor, /新增自定义模块/);
  assert.match(marketEditor, /createOverviewModule\(title\)/);
  assert.match(marketEditor, /addOverviewItem\(items, module\.id\)/);
  assert.match(marketEditor, /removeOverviewItem\(items, module\.id, item\.id\)/);
  assert.match(marketEditor, /removeOverviewModule\(items, module\.id\)/);
  assert.match(marketEditor, /!module\.read_only && !module\.field_key/);
  assert.match(marketEditor, /window\.confirm\(`删除自定义模块/);
  assert.match(marketEditor, /window\.confirm\('删除这条历史内容？'/);
  assert.match(marketEditor, /!editing \|\| module\.read_only/);
});

test('saved analysis reloads its source manifest and conflict handling covers backend HTTP 400 copy', () => {
  assert.match(archivePage, /archive\.market_analysis_run_id/);
  assert.match(archivePage, /savedAnalysisRun/);
  assert.match(archivePage, /<AnalysisSourceSummary manifest=\{savedAnalysisRun\.source_manifest\}/);
  assert.doesNotMatch(archivePage, /<ProjectFiles/);
  assert.doesNotMatch(archivePage, /\/api\/projects\/\$\{workspaceId\}\/files/);
  assert.match(archivePage, /已被其他操作更新/);
});

test('successful whole-analysis decision retires the draft while later manual save remains available', () => {
  assert.match(archivePage, /setOverviewRun\(null\);setOverviewRunId\(''\);sessionStorage\.removeItem/);
  assert.match(archivePage, /setOverviewModules\(createDefaultOverviewModules\(value\)\)/);
  assert.match(archivePage, /const saveOverview=/);
});

test('overview generation defaults to archive category and no longer asks for a research month', () => {
  assert.match(archivePage, /categoryContext\|\|archive\?\.category/);
  assert.match(archivePage, /pa-research-setup/);
  assert.match(archivePage, /开始调研/);
  assert.match(archivePage, /pa-research-date-coverage/);
  assert.doesNotMatch(archivePage, /data_month_context/);
  assert.doesNotMatch(archivePage, /monthContext/);
  assert.doesNotMatch(archivePage, /source_filename.*split/);
});

test('failed overview drafts expose the failed phase, actual usage and offline recovery affordance', () => {
  assert.match(archivePage, /overviewRun\?\.status==='failed'/);
  assert.match(archivePage, /失败阶段：\{overviewPhaseLabel\(overviewRun\.phase\)\}/);
  assert.match(archivePage, /overviewRun\.recovery\?\.available/);
  assert.match(archivePage, /离线恢复分析结果/);
  assert.match(archivePage, /\/overview-drafts\/\$\{overviewRunId\}\/recover/);
  assert.match(archivePage, /\{\},'POST'/);
  assert.doesNotMatch(archivePage, /overviewRun\.raw_output|overviewRun\.raw_result/);
  assert.match(archivePage, /usage\?\.cost_status==='measured'/);
  assert.match(archivePage, /usage\?\.input_tokens/);
  assert.match(archivePage, /usage\?\.output_tokens/);
});

test('direct archive visit loads the latest open analysis without requiring old session storage', () => {
  assert.match(archivePage, /overview-drafts\/latest-open/);
  assert.match(archivePage, /if\(latest\?\.id\)\{setOverviewRunId\(latest\.id\)/);
});

test('overview recovery diagnostics explain missing fields and quality warnings without blocking whole-analysis review', () => {
  assert.match(archivePage, /diagnostics\.missing_fields/);
  assert.match(archivePage, /diagnostics\.module_gaps/);
  assert.match(archivePage, /diagnostics\?\.quality_warnings/);
  assert.match(archivePage, /recovery\?\.reason/);
  assert.match(archivePage, /超过 5 个细分/);
  assert.match(archivePage, /保留整份分析/);
  assert.match(archivePage, /忽略整份分析/);
});
