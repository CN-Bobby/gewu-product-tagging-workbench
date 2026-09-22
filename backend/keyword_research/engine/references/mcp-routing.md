# SellerSprite MCP 路由与调用控制

## 1. 接口路由总表

| 场景 | MCP 函数 | 说明 |
|---|---|---|
| 校验 Node、取完整路径 | `product_node` | 每 Node 一次，取 `nodeIdPath` |
| 取 Node 榜单商品 | `product_research` | 按页拉取，轻量字段优先 |
| 批量反查 ASIN 关键词 | `traffic_extend` | 核心接口，每批 ≤20 个同站点 ASIN |
| 直接输入 ASIN 的两个矩阵 | `traffic_keyword` | 每ASIN当前第1页，同次取自然位/广告位/流量占比 |
| 关键词历史趋势 | `aba_research_trend` | 每入选趋势词一次，`timeGranularity: M` |
| 单 ASIN 流量结构概览（非默认） | `traffic_keyword_stat` / `traffic_source` | 仅用户明确需要时使用 |

历史快照月份来自 `input.json.options.data_month`（`yyyyMM`，省略则为执行当月），只传给 `product_research.month` 与 `traffic_extend.historyDate`。`traffic_keyword` 不传 `month`，固定取得当前数据；`aba_research_trend` 保存接口全部历史并由本地取最新24个连续自然月。

## 2. ASIN 输入（traffic_extend）

- 同站点 ASIN 先去重再分批，每批最多 20 个。
- 默认 `queryType: 1`（畅销变体）；仅当用户明确诊断指定子 ASIN 时用 `queryType: 2`。
- 按 `trafficPercentage` 降序，固定 `size=50`。新任务固定 `keyword_pool_pages=3`，请求第1–3页；即使第2页重复、短页或空页也继续第3页。三页按标准化关键词去重后全部收录，不设100词上限，第3页后停止，禁止第4页。旧 `keyword_pool_size=50|100` 兼容映射为1页|3页。矩阵返回的其他词不得扩入关键词总表、分析或趋势候选。
- 多ASIN批次不返回逐词来源ASIN：只记录批次范围，不得把整批ASIN写成每个关键词的实际覆盖。当前覆盖度只可从逐ASIN矩阵计算。
- 尽量接收完整返回字段；字段清单见 `metric-definitions.md`。
- 每批每页原始返回落盘 `raw/traffic_batch_<批次>_p<页>.json`；旧的无页码文件只作为第1页兼容读取。

## 3. Node 输入流程

1. `product_node` 校验每个 Node ID，取得完整 `nodeIdPath`，落盘 `raw/node_<nodeId>.json`。
2. `product_research` 获取榜单商品，落盘 `raw/products_<nodeId>_p<页>.json`。
3. 请求轻量必要字段：ASIN、父体、子类排名、销量、品牌、标题。
4. **本地复核子类 BSR**：读取返回的子类排名，排除超出 Top N 的商品。不得完全信任 `maxSubBsrRank` 的服务端过滤。
5. 去除重复 ASIN 与重复子变体（`exclude_variations` 默认 true）。
6. 汇总同站点全部有效 ASIN 写入 `cache/asin_pool.json`，每 20 个一批调 `traffic_extend`。

`asin_pool.json` 结构：

```json
{
  "marketplace": "DE",
  "asins": [
    {"asin": "B0XXX", "parent": "B0PARENT", "sources": ["node:12345", "input"], "sub_bsr": 12, "monthly_sales": 340, "brand": "...", "title": "..."}
  ]
}
```

响应截断处理：不重复拉取全部完整字段；改为只取 ASIN、子类排名、销量等轻量字段，只补缺失页，并在 call_log 记录补调用原因。

### product_research 实测参数模式（V1.1/V1.2 实测沉淀，避免重复试错）

真实任务中曾出现连续 3 次返回 0 条的试错（浪费 5 次补调用）。正确姿势：

1. `nodeId` 用 `product_node` 返回的**完整 nodeIdPath 路径字符串**（冒号分隔的全路径，如 `Clothing, Shoes & Jewelry > Girls > Swim > ...` 对应的 ID 路径）；只传单级 nodeId 会返回 0 条；
2. **必须传 `variation: Y`（排除子变体）**：不传时返回的是子体 ASIN 列表，会被单个父体的几十个子变体刷屏（实测 PROALLO 一个父体占满整页 50 条、rank 相同），无法按 BSR 取样；
3. **禁止组合 `filterSub: Y` 或 `maxSubBsrRank` 与 nodeIdPath 同用**：实测该组合会导致 nodeIdPath 过滤失效，返回整个美国市场（total 1425 万，纸巾/咖啡等无关商品），数据全部废弃；rank 过滤一律在本地复核（读 `subcategories[].rank`）；
4. 显式传 `month`（格式 `yyyyMM`，如 `202607`）；
5. 分页用 `page` + `size`（如 size=50），按页拉取直到凑够 Top N 或列表耗尽；
6. 一次调用前先核对 schema 必填项，**参数不确定时先小试 1 页**（page=1, size=20）确认有数据再放量。

注意：`nodeIdPathEqual: true` 仍可能返回兄弟类目商品（如 Rash Guard 子类），本地必须按 `subcategories[].code == 目标类目` 再过滤。

## 4. 直接输入 ASIN 矩阵（traffic_keyword）

- 仅覆盖 `input.json.groups[].asins` 中用户直接输入的 ASIN；纯 Node 模式不调用，Node 榜单样本不得自动扩入。
- 每个ASIN独立请求，**不传 `month`**、固定 `page=1`、`size=100`、按 `trafficPercentage` 降序；每ASIN恰好1次。
- 不设置 `returnFields`：实测精简字段请求可能返回字段全为 null 的单对象，无法作为明细分页使用。
- `total` 已验证为膨胀值且深分页会降级；**禁止读取 `total` 规划后续页，禁止第2页**。当前前100中未匹配到总表的单元格留空，不补取。
- 原始返回保存为 `raw/traffic_keyword_current_<ASIN>_p1.json`，附 `_meta={asin,marketplace,observed_at,page:1,page_size:100,snapshot_type:"current"}`。
- 字段映射：`rankPosition.page/index/position`、`adPosition.page/index/position`、`trafficPercentage`。报告排名只用 `page/index` 格式化为 `P页-页内位`；缺失保持空白，不填0，不从 `position` 反推页码。
- 旧版 `traffic_keyword_<ASIN>_p*.json` 为历史/多页缓存，聚合器必须忽略，不能静默冒充当前数据。
- **限流纪律**：使用真实滑动窗口确保≤8次/60秒；相邻调用不得短于7.5秒。达到窗口上限时持续等待并显示倒计时，任务不得因限流等待而停止。临时限流重试计入全任务最多2次补调用；仍失败则保留缓存并暂停等待用户确认新预算，禁止无间隔重试。

纪律重申：基准外自动补调用总上限2次；同一临时错误最多重试1次。权限、额度、参数错误不重试。`ERROR_UNAUTHORIZED` 只能记录为“授权或额度拒绝”，不得仅凭错误码断言密钥无权限。

## 5. 历史趋势（aba_research_trend）

- 每个入选趋势词调用一次：`marketplace` + `keyword`（原始关键词）+ `timeGranularity: M`。
- 接口通常返回超过 24 个月：**保存完整原始历史**到 `raw/trend_<slug>.json`，由 `analyze_trends.py` 本地对齐与截取。
- 用户后续要求转置、改 12 个月、加图表、加同环比——一律从缓存重算，不重复调用。
- V1 不调用历史月份的 `keyword_miner` / `traffic_extend` 获取历史 PPC。

## 6. 调用量公式

定义：A=同站点去重后的直接输入ASIN数；N=Node数；P_i=第i个Node商品列表页数；U=直接ASIN与Node榜单合并去重后的ASIN数；T=趋势词数。

```
K = keyword_pool_pages；新任务固定3。旧 keyword_pool_size=50|100 兼容映射为1|3
ASIN 模式:   预计调用 = ceil(A/20) × K + A + T
Node 模式:   预计调用 = N + ΣP_i + ceil(U/20) × K + T
混合模式:    预计调用 = N + ΣP_i + ceil(U/20) × K + A + T
```

固定三页时，20个同站点ASIN、无Node、默认10个趋势词固定为 `ceil(20/20)×3+20+10=33` 次，Sorftime为0次；基准外最多2次临时错误补调用，硬上限35。矩阵不再依据第1页 `total` 重算。

补调用：允许最多 2 次自动修复型补调用；预计超出 2 次以上暂停并征求用户确认。

## 7. 调用日志（call_log.json）

每次 MCP 调用前先追加 `status: in_progress`，返回后原位补全 `success`、`returned` 与 `completed_at`：

```json
{
  "calls": [
    {"seq": 1, "tool": "traffic_extend", "scope": "DE batch 1 (20 asins)", "returned": 50, "success": true, "truncated": false, "is_retry": false, "retry_reason": null}
  ],
  "planned_total": 33,
  "hard_cap": 35,
  "actual_total": 33
}
```

## 8. 异常处理

| 异常 | 处理 |
|---|---|
| 响应截断 | 优先同次调用内解析并保存精简字段；无法恢复时只做轻量补取；不重复请求完整响应 |
| Node 过滤失效 | 本地读子类排名排除超 Top N 商品；需补足样本时再翻页；记录实际有效样本数 |
| 趋势缺月 | 保留空白、不补 0、不插值；在数据质量页标记；评估是否影响同比与季节性 |
| ASIN矩阵未匹配 | 空白仅表示未进入当前前100或字段缺失；不继续翻页，不解释为确定无排名/无广告 |
| 关键词指标冲突 | 优先保留最新时间戳数据；不同批次差异明显时记录最大/最小及更新时间；不静默覆盖 |
| `ERROR_UNAUTHORIZED` | 记录“授权或额度拒绝”；不重试；仅在账户状态或用户确认后细分为额度耗尽/权限不足 |
| MCP 不可用/部分失败 | 临时错误仅自动重试一次；权限/额度/参数错误不重复消耗；全任务补调用不得超过2次 |

## 9. 缓存与免调用清单

任务级缓存：`node_lookup`（raw/node_*）、`product_samples`（raw/products_*）、`traffic_batches`（raw/traffic_batch_*）、`traffic_keyword_current`（raw/traffic_keyword_current_*_p1）、`keyword_master`、`asin_keyword_matrix`、`trend_raw`（raw/trend_*）、`call_log`。`keyword_master` 必须记录各页返回数、原始行数、跨页重复数、去重后唯一词数和缺失计划页。

调用前检查：同站点同关键词是否已有完整趋势；同批 ASIN 是否已调用；同 Node 是否已有完整路径；用户需求能否由已有原始数据计算。

以下操作**不得**新增SellerSprite调用：两个矩阵拆分/合并；表格转置；12↔24个月切换；增加同比/环比/图表；修改中文翻译；调整关键词分类；修改Excel颜色和排序；重新生成摘要。
