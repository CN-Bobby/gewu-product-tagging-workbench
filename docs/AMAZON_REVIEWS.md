# Amazon 评论采集与翻译

## 来源与契约

工作台实现依据私有仓库 CN-Bobby/Bobby_Skills_Group/sorftime-reviews-pull，源码固定在提交 ef447da2b9785c8ac921350c706dd858b18794f2。该 Skill 的接口与流程约定：

- 榜单数据从 Sorftime category_report 的 data.top100_products 读取。
- 评论由 product_reviews 提供，站点使用 Amazon marketplace code。
- 每个 ASIN 以 review_type=Both 调用两次，按 (ASIN, review_date, title, content) 去重。两次调用是重复采样，不是分页。
- 每次最多 100 条且范围为最近一年；不能将结果称为全历史评论。
- 美国站可以在页面中选择 Sorftime MCP 或当前项目的 Woot 评论采集器。Woot 采集按 5 个星级和 4 种排序组合请求并翻页、去重；评论量较大时耗时会明显增加。Woot 使用非官方接口，接口可能变化。
- Woot 仅支持 Amazon 美国站。英国、德国等其他站点仍使用 Sorftime MCP；选择 Woot 时若输入方式需要类目榜单，Sorftime 仍用于取得商品 ASIN。直接输入 ASIN 时，Woot 评论采集不要求配置 Sorftime。
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
- Excel 工作表为「全部评论 / 商品评论统计 / 统计汇总」；首表包含 14 个源字段并支持筛选与情感着色。

## 工作台接入

费用优化分支的前端入口为主导航「Amazon 评论 + VOC」，页面路由为 `#/amazon-reviews-voc`；页面保留评论采集表单，并提供评论拉取 + VOC、仅评论拉取、仅 VOC 三种目标。评论采集提供三种输入方式：类目榜单 Top 50/100、直接输入 ASIN、从类目榜单筛选指定 ASIN。站点、模型及已授权模型 Key 均在任务创建时冻结。
前端入口为主导航「Amazon 评论」，页面路由为 #/amazon-reviews。页面提供三种输入方式：类目榜单 Top 50/100、直接输入 ASIN、从类目榜单筛选指定 ASIN。站点、评论来源、模型及已授权模型 Key 均在任务创建时冻结。来源选择仅在美国站显示；API 对非美国站的 Woot 请求会拒绝处理。

Sorftime MCP 配置复用工作台「寻龙诀」的设置；翻译模型与凭据复用「总设置」。页面仅检查连接是否配置，不读取或显示密钥。任务和逐商品检查点保存在当前 TAG_STORAGE/amazon-reviews/<job-id>/，支持进度查询、取消、重试和重启后恢复。单次最多并行两个评论任务。

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
- POST /api/amazon-reviews/jobs、GET /api/amazon-reviews/jobs
- GET /api/amazon-reviews/jobs/{job_id}
- POST /api/amazon-reviews/jobs/{job_id}/cancel
- POST /api/amazon-reviews/jobs/{job_id}/retry
- GET /api/amazon-reviews/jobs/{job_id}/download
- GET /api/amazon-reviews/jobs/{job_id}/raw

导出保留各来源的完整原始响应、采集方式、原始/新增去重数量、翻译结果及汇总工作簿。Sorftime 的两个 Both 调用仍按已保存轮次恢复；Woot 的一次采集批次包含多个星级/排序请求。

## 验证

离线回归：

    python -m pytest tests/test_amazon_reviews.py -q

前端回归与构建：

    npm test
    npm run build

美国站真实端到端验证（脚本只接受 stdin 中的一行 Sorftime Key；Key 会写入独立测试目录，退出时清除。模型 Key 从现有总设置解析，不写入任务记录）：

    python scripts/verify_sorftime_reviews_live.py --key-stdin

该脚本在仓库忽略目录 builds/sorftime-reviews-live/<run-id>/ 创建独立 SQLite、任务数据、Excel、原始 JSON 和验证报告，不使用正式或绿色工作台数据目录。默认使用已确认位于美国站 Node 17269957011 的 ASIN B0D5TQJXRN，调用 1 次榜单接口和 2 次评论接口；可用 --asins 追加多个同类目 ASIN。遇到可重试失败时通过工作台重试路由补齐未完成工作，最多重试两次。最终校验数据去重、译文覆盖、三张工作表及计数一致性。

若某次真实任务已完成评论采集、但后续翻译失败，可将已清除凭据的隔离任务安全恢复：

    python scripts/verify_sorftime_reviews_live.py --key-stdin --resume-run-id 20260929T115704Z-86ac57ef

恢复模式从该 run 的请求与任务检查点读取站点、Node、ASIN 和任务 ID，只重试未完成商品；验证器会断言恢复期间新增评论 API 调用数为 0，并另存恢复报告，不覆盖首次运行报告。
