import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=await readFile(new URL('../src/Xunlongjue.jsx',import.meta.url),'utf8');
const stateSource=await readFile(new URL('../src/xunlongjueState.js',import.meta.url),'utf8');

test('new scans expose category, operator, node cap, and all eight original policy inputs',()=>{
 for(const label of ['类目名称','操作人','最大节点数','近6月均销门槛','最大层级','销量下降容忍','价格下降容忍','最低 R²','批量','最大尝试次数','最大数据滞后月'])assert.ok(source.includes(label),`missing ${label}`);
 for(const key of ['min_avg_sales','max_level','sales_decline_tolerance','price_decline_tolerance','min_r_squared','batch_size','max_attempts','max_data_lag_months'])assert.ok(source.includes(key),`missing policy key ${key}`);
 assert.match(source,/node_id:nodeId\.trim\(\),category:category\.trim\(\),operator:operator\.trim\(\),policy:\{\.\.\.policy\}/);
});

test('run list supports server-side filters, ordering, and archived or trashed scopes',()=>{
 assert.match(source,/queryString\(\{search,status:statusFilter,sort,direction,archived:scope==='archived',trashed:scope==='trashed'\}\)/);
 for(const label of ['搜索任务','状态筛选','任务范围','排序字段','切换排序方向'])assert.ok(source.includes(label),`missing ${label}`);
});

test('the latest list request always clears loading even when it supersedes the initial request',()=>{
 assert.match(source,/finally\{if\(request===listRequest\.current\)setRunsLoading\(false\)\}/);
});

test('metadata uses optimistic revision and named presets expose create, update, delete, and apply',()=>{
 assert.match(source,/\/metadata`,\{revision:Number\(selectedRun\.revision\)\|\|1,name:nameDraft,note:noteDraft\}/);
 assert.match(source,/mergeRunByRevision/);
 assert.match(source,/setDetail\(current=>mergeRunByRevision\(current,value\)\)/);
 assert.match(source,/setRuns\(current=>mergeRunsByRevision\(current,rows\)\)/);
 assert.match(stateSource,/export function mergeRunByRevision/);
 assert.match(stateSource,/currentUpdated>incomingUpdated/);
 assert.match(source,/await api\(PRESETS,body\)/);
 assert.match(source,/await api\(`\$\{PRESETS\}\/\$\{encodeURIComponent\(presetId\)\}`,body,'PUT'\)/);
 assert.match(source,/await api\(`\$\{PRESETS\}\/\$\{encodeURIComponent\(presetId\)\}`,undefined,'DELETE'\)/);
 assert.ok(source.includes('应用预设'));
});

test('bulk lifecycle actions require all selected active runs to be paused and restore is explicit',()=>{
 assert.match(source,/chosen\.some\(run=>activeStatuses\.has\(runStatus\(run\)\)\)/);
 assert.match(source,/api\(`\$\{RUNS\}\/bulk`,\{action,run_ids:selectedIds\}\)/);
 assert.ok(source.includes('批量恢复'));
 assert.ok(source.includes('恢复任务'));
 assert.ok(source.includes('请先暂停正在运行的任务'));
});

test('incomplete scans surface a rerun alert with parameter-copy action',()=>{
 assert.match(source,/selectedRun\.needs_rerun\|\|selectedRun\.incomplete\|\|selectedRun\.node_cap_reached\|\|selectedRun\.completion_ok===false/);
 assert.ok(source.includes('扫描结果不完整，需重新运行或续跑'));
 assert.ok(source.includes('复制参数到新扫描'));
});
