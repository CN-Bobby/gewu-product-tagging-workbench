# Amazon 评论采集与翻译

## 来源与契约

工作台实现依据私有仓库 CN-Bobby/Bobby_Skills_Group/sorftime-reviews-pull，源码固定在提交 ef447da2b9785c8ac921350c706dd858b18794f2。该 Skill 的接口与流程约定：

- 榜单数据从 Sorftime category_report 的 data.top100_products 读取。
- 评论由 product_reviews 提供，站点使用 Amazon marketplace code。
- 每个 ASIN 以 review_type=Both 调用两次，按 (ASIN, review_date, title, content) 去重。两次调用是重复采样，不是分页。Sorftime 返回记录没有稳定评论 ID；相同去重键合并为一条时，会把所有不同的变体属性保留在导出行中。无法仅凭这些字段判定同日同标题同正文的记录是否来自不同评论。
- 每次最多 100 条且范围为最近一年；不能将结果称为全历史评论。
- 美国站默认使用自动来源：直接输入 ASIN 时先尝试 Woot。确认目录无精确匹配、候选页达到 200 页上限、或目录读取不完整时，自动模式会明确记录原因并改用 Sorftime MCP；不会把不完整检索伪装成已确认无匹配。Woot 评论接口错误、网络异常和离线模式拒绝外部调用仍保留为失败。用户也可选择仅 Sorftime 或仅 Woot。
- Woot 采集按 5 个星级和 4 种排序组合请求并翻页、去重；单个 ASIN 可能产生 20 次以上请求，评论量较大时耗时会明显增加。Woot 使用非官方接口，接口可能变化。
- Woot 单个 ASIN 最多进行 500 次 HTTP 请求尝试，Amazon 商品页、Woot 目录页、评论请求和重试共用此上限；取消会在下一次请求前生效。结果分别记录总请求、目录解析请求和评论请求数。
- 自动匹配按 Amazon 商品标题排序候选页，最多检查 200 个 Woot 商品页；仍受上述 500 次总尝试预算约束，并且只有商品页含精确相同 ASIN 才算匹配。
- Woot 仅支持 Amazon 美国站。英国、德国等其他站点仍使用 Sorftime MCP；选择 Woot 时若输入方式需要类目榜单，Sorftime 仍用于取得商品 ASIN。直接输入 ASIN 时，Woot 评论采集不要求配置 Sorftime。
- Woot 直接输入 ASIN 时，工作台先读取 Amazon 美国站商品标题，再按标题从 Woot 公开商品站点地图中筛选候选商品页；打开候选页后必须在 `offerItems` 中验证完全相同的 ASIN，才会读取其 `offerId`。候选检索有上限，因此只支持当前公开目录中可匹配到的在售/存档商品，不是 Amazon 全 ASIN 评论服务。自动模式在目录确认无匹配、检索范围达到上限或目录读取不完整时回退到 Sorftime，并保留具体原因；自动和仅 Woot 模式均可粘贴对应 Woot 评论链接（含 `itemid` 和 `offerid`）。Woot 评论 AJAX 请求使用 `offerid`；不把 `itemid` 加进请求参数。榜单和类目输入由 Sorftime 获取商品与评论，避免按大批 ASIN 逐一扫描 Woot。
- Woot ASIN 匹配使用公开 Amazon 商品页和 Woot 站点地图，不需要 Woot Developer API Key。该匹配只覆盖当前公开 Woot 目录中能找到的商品；官方 Developer API Key 用于 Feed 等接口，也不是任意 Amazon ASIN 的通用评论服务。官方接口说明见 [Woot Developer Portal](https://developer.woot.com/)。
- Woot 采集实现参考 [mrlong0129/amazon-review-scraper](https://github.com/mrlong0129/amazon-review-scraper)，按其 MIT License 使用；源码文件保留了许可说明。

  ```text
  MIT License

  Copyright (c) 2026

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
  ```
- 对返回的每条评论翻译非空标题和正文；按星级映射情感：4–5 星积极、1–2 星消极、3 星中性。
- Sorftime 每个 ASIN 最多返回最近一年内 100 条；`Both` 两次调用是重复采样而不是分页。它和 Woot 的覆盖范围不同，自动回退提高可用性，但不能保证取得 Amazon 上某个 ASIN 的全部公开评论。
- 可在美国站“指定 ASIN”模式明确选择 Bright Data 评论 API，作为 Woot/Sorftime 之外的补充。它使用 Amazon 商品 URL，每个 ASIN 在请求中硬性限制最多 20 条；不会加入自动回退，也不会在未选择时调用。提交前会再次提示本批次的最大评论数和服务商可能产生的费用。服务商账户 Key 在「总设置」中保存至当前 Windows 用户 DPAPI 凭据库；Key 不会写入任务文件或返回状态。该渠道尚需在配置 Key 后完成真实 API 样本验证，因此在实时验证前不能承诺任意 ASIN 均有评论或采集成功。
- Excel 工作表为「全部评论 / 商品评论统计 / 统计汇总」；「全部评论」含 15 列并展示每条评论的实际采集来源，商品统计也显示每个 ASIN 的来源。

## 工作台接入

费用优化分支的前端入口为主导航「Amazon 评论 + VOC」，页面路由为 `#/amazon-reviews-voc`；页面保留评论采集表单，并提供评论拉取 + VOC、仅评论拉取、仅 VOC 三种目标。评论采集提供三种输入方式：类目榜单 Top 50/100、直接输入 ASIN、从类目榜单筛选指定 ASIN。站点、模型及已授权模型 Key 均在任务创建时冻结。
前端入口为主导航「Amazon 评论」，页面路由为 #/amazon-reviews。页面提供三种输入方式：类目榜单 Top 50/100、直接输入 ASIN、从类目榜单筛选指定 ASIN。美国站默认自动来源：指定 ASIN 时先查 Woot，目录无匹配、检索达到上限或目录读取不完整时回退 Sorftime；榜单/类目模式由 Sorftime 处理。用户也可选择仅 Woot、仅 Sorftime，或明确指定 Bright Data。Bright Data 仅用于直接 ASIN 模式，不自动回退；每批最多 100 个 ASIN、每个 ASIN 最多 20 条，提交前显示最大返回量和费用提醒。Bright Data Key 需在总设置中单独配置。自动模式需要 Sorftime 已配置，才可保证回退路径可用。站点、评论来源、模型及已授权模型 Key 均在任务创建时冻结。来源选择仅在美国站显示；API 对非美国站的 Woot/Bright Data 请求会拒绝处理。商品打标页“数据流转”推送也提供美国站评论来源选择并将选择传给任务。

Sorftime MCP 配置复用工作台「寻龙诀」的设置；Bright Data 与翻译模型凭据由「总设置」管理。页面仅检查连接是否配置，不读取或显示密钥。任务和逐商品检查点保存在当前 TAG_STORAGE/amazon-reviews/<job-id>/，支持进度查询、取消、重试和重启后恢复。单次最多并行两个评论任务。

任务记录保存创建时间、每次执行的开始/结束时间和耗时；重试历史分别保留在 `attempt_history`。`duration_seconds` 是最近一次执行耗时，`total_duration_seconds` 是所有已结束执行耗时之和，不包含排队等待。排队取消时没有执行耗时；工作台重启后标记的中断时间是恢复检查时点，不是断电或崩溃的精确时刻。完成时间和累计执行时间会显示在任务卡片上。

### Reviews + VOC 费用优化测试版

在实验分支 `experiment/reviews-voc-cost-optimization` 中，Amazon 评论与 VOC 使用统一工作流页，提供默认“评论拉取 + VOC”、仅评论拉取、仅 VOC 三种目标。新建组合任务在评论 Excel 生成后自动启动 **VOC V2 一次性完整分析**；人工检查后交接和独立 VOC 上传也进入 V2。VOC 失败后可从已保存的评论结果继续，不重新拉取。仅 VOC 使用单份评论 Excel。评论翻译默认开启，可在任务设置中关闭。旧任务及显式标记为 `multi_stage_v4` 的回归仍使用原多阶段流水线。

工作区可复用 7 天内的评论结果、翻译和 VOC 阶段结果；强制刷新会跳过本地结果复用。缓存键绑定输入、模型、提示词、schema 和设置版本。费用账本记录评论 API 调用及新增评论数、各模型阶段的输入/输出和缓存 token、重试次数；本地复用请求数单独记录。未收到供应商费用数据时不估算实际账单。

确定性集成测试覆盖三个目标、默认自动串接、VOC 失败续跑、缓存命中/失效、翻译开关及阶段费用记录。完整回归可运行：

```powershell
python -m pytest tests/test_reviews_voc_cost_workflow.py tests/test_amazon_reviews.py tests/test_amazon_voc_reviews_bridge.py tests/test_amazon_voc_usage_metering.py -q
npm --prefix frontend test
npm --prefix frontend run build
```

VOC V2 在一次请求中完成评论覆盖、事件、动态词典与映射、细分、归属、旅程和报告数据，输出仍经过本地结构与证据校验，再生成原有六模块 HTML 和 12 工作表 Excel。独立语义复核保持待处理，必须人工检查后才能验收。V2 的确定性测试不会发起模型请求，也不证明大样本上下文容量。

该分支是隔离测试版，不代表已经并入正式工作台或发布。

API 路由：

- GET /api/amazon-reviews/settings
- GET/PUT/DELETE /api/settings/brightdata
- POST /api/amazon-reviews/jobs、GET /api/amazon-reviews/jobs
- GET /api/amazon-reviews/jobs/{job_id}
- POST /api/amazon-reviews/jobs/{job_id}/cancel
- POST /api/amazon-reviews/jobs/{job_id}/retry
- GET /api/amazon-reviews/jobs/{job_id}/download
- GET /api/amazon-reviews/jobs/{job_id}/raw

导出保留各来源的评论结果、任务所选方式、每个 ASIN 和评论实际使用的来源、自动回退原因、原始/新增去重数量、翻译结果及汇总工作簿。Sorftime 的两个 Both 调用仍按已保存轮次恢复；Woot 的一次采集批次包含多个星级/排序请求；Bright Data 每个 ASIN 以单个有上限的采集快照处理。自动回退决定和实际来源写入逐 ASIN 检查点，重试时不重新尝试已确认缺少精确 Woot 匹配的来源。

## 验证

离线回归：

    python -m pytest tests/test_amazon_reviews.py -q

前端回归与构建：

    npm test
    npm run build

美国站真实端到端验证（脚本只接受 stdin 中的一行 Sorftime Key；Key 会写入独立测试目录，退出时清除。模型 Key 从现有总设置解析，不写入任务记录）：

    python scripts/verify_sorftime_reviews_live.py --key-stdin

该脚本在仓库忽略目录 builds/sorftime-reviews-live/<run-id>/ 创建独立 SQLite、任务数据、Excel、原始 JSON 和验证报告，不使用正式或绿色工作台数据目录。运行时必须显式传入美国站 Node 和 ASIN（支持多个）；脚本不会预设商品目标。调用榜单和评论接口会访问真实 Sorftime/Amazon 服务，并可能产生费用。遇到可重试失败时通过工作台重试路由补齐未完成工作，最多重试两次。最终校验数据去重、译文覆盖、三张工作表及计数一致性。

若某次真实任务已完成评论采集、但后续翻译失败，可将已清除凭据的隔离任务安全恢复：

    python scripts/verify_sorftime_reviews_live.py --key-stdin --resume-run-id 20260929T115704Z-86ac57ef

恢复模式从该 run 的请求与任务检查点读取站点、Node、ASIN 和任务 ID，只重试未完成商品；验证器会断言恢复期间新增评论 API 调用数为 0，并另存恢复报告，不覆盖首次运行报告。
