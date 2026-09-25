# WorkBuddy 调用协议 v3

## CLI 回执与参数映射

WorkBuddy 调用 `doctor/start/next/ingest/pulse/status/report` 时传入不重复的 `--response-file`。CLI 会先原子写入 UTF-8 JSON，再保留原 stdout 输出；客户端捕获不到 stdout 时直接读该文件。执行期错误也写入 `{ "ok": false, "error": "...", "exit_code": 2 }`。并发对话不得共用回执路径。

自然语言与 CLI 名称固定映射：

- 近6月均销门槛 → `--volume-threshold`（兼容别名 `--min-avg-sales`）
- 最大层级 → `--max-level`
- 销量/价格下降容忍 → `--sales-decline-tolerance` / `--price-decline-tolerance`
- 最低 R² → `--min-r-squared`
- 批量/重试/数据滞后 → `--batch-size` / `--max-attempts` / `--max-data-lag-months`

不得将数据库字段名 `min_avg_sales` 直接猜成未定义的 CLI 参数。

## next 输出

新 Run 的第一次 `next` 只返回根节点 `category_tree`。根节点不生成 `category_trend`；类目树返回的直接子节点才各自生成 SalesCount 与 AvgPrice 任务。这是业务协议，不得由 WorkBuddy 调整顺序。

```json
{
  "schema_version": "3",
  "run_id": "uuid",
  "batch_id": "uuid",
  "jobs_path": "...\\ipc\\RUN_ID\\BATCH_ID\\jobs.json",
  "results_draft_path": "...\\results.draft.json",
  "results_path": "...\\results.json",
  "jobs_count": 2,
  "jobs": [{
    "job_id": "uuid",
    "type": "category_tree|category_trend",
    "site": "US",
    "node_id": "123",
    "indicator": "SalesCount|AvgPrice|null",
    "arguments": {},
    "attempt": 1,
    "lease_token": "uuid",
    "lease_until": "UTC ISO-8601",
    "batch_id": "uuid"
  }]
}
```

不要修改任务字段或自行选择文件名。租约 30 分钟；过期令牌和旧批次结果会被拒绝，以避免旧对话覆盖新结果。不同 Run 和 Batch 的 IPC 路径完全隔离，与发起对话时的文件夹无关。

## ingest 输入

```json
{
  "schema_version": "3",
  "run_id": "uuid",
  "batch_id": "uuid",
  "results": [{
    "job_id": "uuid",
    "attempt": 1,
    "lease_token": "uuid",
    "status": "success|failed",
    "payload": {},
    "error": ""
  }]
}
```

把完整 JSON 写入 `next` 返回的 `results_draft_path`，关闭文件后执行 `ingest --run RUN_ID --batch BATCH_ID`。同一批中的每个 job 都必须恰好有一个结果，不得缺少、增加或重复。整批要么全部写入，要么全部回滚。完全相同的重复提交幂等返回原 `receipt.json`；过期批次拒绝写入。每完成 2 项或距上次心跳超过 10 秒时必须调用 `pulse`，批次末尾再调用一次；它不参与业务结果事务。

同一个 Run 同时只允许一个活动 Batch。再次调用 `next` 会返回 `busy: true` 和现有批次路径，不会重复租出 Job。批次安全由 SQLite 事务、`batch_id + job_id + attempt + lease_token` 保证，不再由对话框所有权或 `worker_id` 保证。

升级前已经租出的协议 v2 任务可以用原 `--input` 和原令牌回写一次；所有新领取任务只生成协议 v3。

## category_tree

成功 payload 是直接子类目数组：

```json
[{"id":"456","name":"Storage"}]
```

也接受包含 `items`、`subcategories` 或 `data` 数组的对象。空数组表示叶子节点。禁止混入孙级节点或父节点自身。

根节点空数组表示扫描起点没有直接子类目，根节点记录为已展开但不构成机会。普通趋势通过节点空数组才表示末端机会。

## category_trend

```json
{"months":["2026-01","2026-02"],"values":[12000,12500]}
```

必须为 6–24 个连续月份，数值有限，SalesCount 与 AvgPrice 月份完全一致，最新月不得超过策略允许的滞后。24 个月走正式双路径判定；6–23 个月走短历史规则。

## 进度和结束

根节点固定为 L1，根的直接子节点固定为 L2。层级播报必须使用节点 `level` 或 `level_funnel`，不得根据“第几批”推测；第一轮子节点趋势是 L2，不是 L1。

- `RUNNING`：继续领取；无任务时可能处于退避等待。
- `ALL_DONE`：所有分支均到达确定终态，可生成最终报告。
- `PARTIAL_DONE`：没有活动任务，但存在 `SUSPENDED`，只能生成阶段报告。
- `EXPANDED` 表示已获取直接子节点，不是机会。
- `POLICY_PRUNED` 表示按体量策略停止，后代未知。
- `SHORT_HISTORY` 是有效终态，不阻塞 `ALL_DONE`。
- `OPPORTUNITY` 只表示正式机会。

`status --json` 的 `insights` 与报告“扫描过程洞察”完全同源。
