# 格物商品打标工作台：接力开发交接文档

更新时间：2026-09-17（Asia/Shanghai）；上一版：2026-09-16（Agent 运行时迁移）

工作目录：`D:\数据打标项目`

当前开发分支：`feature/0.5.0-delivery`

## 0. 文档用途与事实口径

本文供下一位 Agent 在同步整个项目目录后直接接力开发。内容合并了：

- 当前任务中的 beta.6 构建、安装和两项真实模型测试反馈；
- Codex 任务 `01a09369-2a38-77e0-ba73-69342c6f94a4`（“01梳理商品打标产品需求”）；
- Codex 任务 `01a07538-55d5-7672-8a86-42d3821c2dc7`（“00实现AI商品批量打标工具”）；
- 当前源码、版本台账、本机健康接口和正式数据只读画像。

接力时按以下优先级判断事实，不能只依据历史对话：

1. 当前源码、自动化测试和正在运行的健康接口；
2. `VERSION.json`、`RELEASE_LEDGER.json`、`docs/DELIVERY_STATUS.md` 和对应验收记录；
3. `docs/BETA6_TEST_FEEDBACK-20260913.md` 中的真实失败证据；
4. 历史对话中的产品决策和过程背景。

历史对话可能描述当时状态；版本号、分支、数据库、安装状态和测试数必须重新现场核对。

## 1. 一分钟接管摘要

这是一个 Windows 本地 Excel 商品批量打标工作台：导入商品表格，左侧与 Reasonix 讨论标签，右侧在一张持续草稿工作表中累积标签，后台直接调用已配置模型做批量文字或视觉判断，最后保存不可变版本并导出 Excel。

当前状态不是“beta.6 正式完成”，而是：

- beta.6 候选代码已构建；本机运行 `0.5.0-beta.6+2f7bf2b59975`（**该安装包不含本文档之后的任何源码修复**）；
- 两个 P0（F01 截断反推、F02 对话零反馈）已于 beta.6 修复，证据见 `docs/BETA6_P0_VERIFICATION.md`；
- beta.6 仍为 `pending_acceptance`，不是正式发布，未创建 tag/Release；当前候选线已推进到 **0.5.0-beta.7**（本机安装的仍是 beta.6）；
- 2026-09-16～17 在本地分支完成了 Agent 运行时迁移与 M6 Runtime Hardening（M1–M6，本地共 13 个提交），**尚未推送**（`origin/feature/0.5.0-delivery` 仍停在旧的 `1ceb4ac`）、**尚未打包**；
- 当前源码后端全量测试为 **596 passed**（`docs/REAL_ACCEPTANCE_CHECKLIST.md` 记录了它证明什么、不证明什么）。

M6 Runtime Hardening 已收口（见 §5.5），**不再新增 runtime milestone**。接力后的首要目标是按 `docs/REAL_ACCEPTANCE_CHECKLIST.md` 执行真实验收与交付，而不是继续做 legacy 清理或新的硬化。

## 2. 产品定义

### 2.1 核心用户价值

用户已经有真实商品 Excel、图片、文本字段以及部分人工判断，希望在不破坏原始数据和人工成果的前提下：

- 用自然语言讨论要判断什么；
- 让系统从真实商品证据和人工样例中形成可执行标签标准；
- 先审核样本和边界，再批量填表；
- 对结果、来源、冲突、失败和成本可追溯；
- 在同一张累积工作表上持续新增、修订和聚合标签；
- 主动保存 V1、V2 等正式版本，而不是每轮生成一份割裂结果。

### 2.2 固定产品心智模型

```text
项目
└─ 工作表/数据集
   ├─ 共享：标签标准、持续草稿、单元格结果、任务、正式版本
   └─ 多个网页对话
      ├─ 独立：聊天消息、附件编号、问题卡、输入草稿、模型选择
      └─ 一个或多个可替换的 Reasonix 原生运行会话
```

关键边界：

- 网页对话是用户可见、持久化的业务对话；
- Reasonix 原生会话是可替换的运行时，不是业务事实唯一载体；
- 后台批量任务是独立、可恢复的执行单元，不能依赖一个无限等待的聊天回合；
- 同一工作表的多个对话共享已经确认的工作成果，但不自动读取彼此的聊天内容和附件编号；
- 模型或 Key 切换不应让用户失去网页对话上下文，只应建立新的原生运行会话并注入受控摘要；
- 新建网页对话意味着新的聊天上下文，但仍可读取当前工作表、已确认标签和任务状态。

### 2.3 Agent、MCP 与批量执行的职责

|层|职责|不应承担|
|---|---|---|
|Web 工作台|展示、提问、人工确认、编辑、版本、恢复入口|不能用模型文字代替正式批准|
|Reasonix coordinator|理解目标、读取工作表、提出标准/任务建议、最多提出三个关键问题|不能直接写正式结果、绕过确认、任意访问本机|
|`product_workbench` MCP|受控读取、画像、范围查询、业务问题、提案|不能提供任意 shell、文件系统或批准工具|
|后台 batch/vision|使用冻结输入直接调用模型、断点保存、并发、缓存、写入候选结果|不能为每件商品新建 Reasonix 对话|
|Skill|提供证据、实体范围、校准、未知、成本和输出契约|不是无限品类百科，不能替商品证据下结论|

当前 Reasonix 基线为 1.25.1。运行时明确允许 `product_workbench` 工具，禁止 `bash`、`write`、`edit`、任意文件读取和其他 MCP。被禁止的调用应转成准确、可恢复的产品错误，不能让 Agent 自行遍历本机寻找替代数据。

## 3. 已确认的核心业务规则

### 3.1 数据、草稿和版本

- 先上传数据，再聊天；Agent 从数据接入后就是主流程的一部分。
- 原始 Excel 永不原地改写，保存在数据目录 `originals`。
- 所有标签在一张持续草稿中累积；讨论、确认、修复、新标签和聚合交错进行。
- 普通聊天中模型说“已经确认”不算批准；网页提案按钮才是业务确认入口。
- 用户主动保存正式版本，生成 V1、V2；保存后的定义、结果、证据和人工修改冻结。
- 模型输出始终是建议，人工修正优先。

### 3.2 已有人工标签“接着打”

- 可把上传表格中的已有列接入为标签列，不另建重复同名列。
- 非空原值标记为导入人工标签并锁定；默认只填空白。
- 只有 `None`、空字符串和纯空格是空白；`0`、`False`、“无”、“未知”都必须保护。
- 重判或覆盖人工值必须是独立、明确、可审计的操作。
- 多值分隔符先预览、后确认，不能擅自按逗号或斜杠拆分。
- 同义、错别字、异常值只作为建议或待复核线索，默认保留原始写法。
- 导出回原列位置，不生成“原列 + AI同名列”。

### 3.3 标签标准反推

用户不应先写完整定义。正确流程是“模型反推标准 → 人工审核/修正 → 正式接入”：

1. 在人工定义输入框附近提供“反推定义”。
2. 用户选择来源标签列和反推所需数据列；已有人工值由系统自动提取，不再重复手填。
3. 程序先统计已有值、空白比例、多值线索、同义/错字/异常线索和可用图片数。
4. 首轮最多 24 件，优先覆盖每个已有值，并加入至少约三分之一未标商品，用于发现人工样本未覆盖的新类型。
5. 标签内差异大、标签间重叠、冲突或新类型较多时可扩至 60 件；扩样前显示商品数、图片数和预计请求量。
6. 模型输出判断目标、单选/多选、已有值规则、模型发现的新候选、正向特征、排除边界、未知/不适用/图片不可判断策略、视觉适用性和最多三个关键问题。
7. 一致性问题只标记待复核，不自动改写人工值。
8. 审核页展示定义、逐项边界、代表图、冲突、未解决问题、样本覆盖、模型、图片范围和生成时间。
9. 用户可编辑、补看样本或回答问题；只有“确认接入”后才创建或修订正式标签列。
10. 人工已有值只是局部参考，不得视为最终完整候选集合。

### 3.4 图片与视觉判断

- WPS `DISPIMG` 需解析 OOXML `xl/cellimages.xml`、关系文件和 `xl/media/*`；不能只看 `openpyxl worksheet._images`。
- 对话支持上传、多图、剪贴板粘贴、拖入和“图1、图2”稳定引用；图片编号只在当前网页对话内有效。
- 对话图片默认只用于讨论；显式绑定商品并确认输入策略后，才成为批量任务补充证据。
- 正式视觉标签默认使用每件商品一张主图；复杂策略必须进入冻结配置和缓存指纹。
- 视觉判断先做主体/实体识别，再做颜色、印花等属性观察，最后按用户确认的标签标准映射。
- 模特、背景、包装、展示道具、套装组成部分、随售配件、多变体合集和前后视图必须区分。
- 颜色需区分底色、拼接色、边缘/滚边、内侧、点缀和图案色；印花需区分拼色、渐变、重复印花、局部图案和纹理。
- 建议分别记录主体识别、视觉观察和标签映射置信度，区分“没看清”与“定义无唯一答案”。

### 3.5 执行和成本

- 默认每批 5 件、3 个并发请求；不要逐商品启动 Reasonix 会话。
- 每次任务由用户选择直接执行或先审样本；历史上曾有固定 20 件门禁，当前产品方向是把是否抽样交给用户，但高风险或大范围任务仍须明确提醒。
- 文本、视觉、模型、Key、Skill/模块哈希、标签修订、图片策略和商品范围在任务提交时冻结。
- 暂停/取消只停止继续派发，已经发送的付费请求可能仍会返回；成功结果必须保存。
- 失败修复只处理失败单元格或缺失区块，不整批原样重试。
- 成本指标最终应包括输入/输出 token、图片数和分辨率、重试、缓存、耗时、估算费用、每千件成本和每个成功标签单元格成本。

## 4. 用户沟通习惯与产品偏好

下一位 Agent 必须遵守：

- 默认中文输出，先说结论和当前真实状态，再讲原因与方案。
- 用户处于产品实测阶段时偏好“先集中倒反馈”：允许连续发截图和零散问题，先逐条记录，不要每条都强迫排序或反复提问。
- 需要提问时最多三个真正影响结果的问题；已经说过的偏好不要重复确认。
- 用户重视证据与推断分离。应给出真实请求数、视觉请求数、耗时、错误码、finish reason、代码位置、构建提交和安装状态。
- 不把程序探针、权限、超时或协议问题归因于模型、Key或用户操作。
- 失败后必须给可恢复路径；已有成功阶段和付费结果不能因后续步骤失败而清空。
- 不静默清洗标签、不覆盖人工值、不自动批准提案、不自动扩样或重打正式数据。
- UI 需要可控制：能新建对话就必须能删除、恢复和彻底删除；运行中删除须说明共享成果保留范围。
- 对话区应降噪：批量执行中不自动弹出大量拆列卡；候选特征应聚合，任务结束后一次汇总。
- 用户要求“做完让我测试”时，通常期待实现、自动验收、候选安装包、冒烟、本机覆盖升级和清晰回退点，不是只给代码片段。
- 对长任务，数秒内给第一条可见状态；之后持续展示阶段和最近活动，不能让界面长时间只显示“正在回复”。

安全偏好：历史对话中曾出现完整 API Key。交接材料不得复制该值；该 Key 应视为已暴露并由用户撤销/轮换。源码、日志、模型尝试、任务快照和安装包均不得保存完整凭据。

## 5. 工程架构与关键文件

### 5.1 技术栈

- 后端：Python 3.14、FastAPI、SQLAlchemy、SQLite。
- 前端：React + Vite，生产静态文件打入安装包。
- Agent：Reasonix CLI 1.25.1，ACP JSON-RPC；业务能力经 `product_workbench` MCP。
- Windows 交付：内嵌 Python、Reasonix 和依赖，由 NSIS 构建当前用户安装器。
- 默认本机地址：`http://127.0.0.1:8765`。

### 5.2 主要代码地图

|文件|职责|
|---|---|
|`backend/app.py`|FastAPI 入口和全部 HTTP/SSE 路由|
|`backend/db.py`|数据库模型、schema 迁移和迁移前一致备份|
|`backend/importer.py`、`backend/image_inputs.py`|Excel/WPS 图片导入、图片资产和主图选择|
|`backend/draft.py`、`backend/draft_export.py`|持续草稿、提案、版本、单元格编辑和导出|
|`backend/chat_threads.py`、`backend/chat_images.py`|多对话、回收站、附件编号和商品绑定（历史查看面；网页聊天运行时已在 M5-4 退役）|
|`backend/harness.py`|Reasonix 启动与运行时目录配置：指令文本、`reasonix.toml`（MCP allow/deny、调用上限）、trusted env、可执行探测。**不含任何运行时客户端**|
|`backend/agent/`、`backend/agent_application.py`、`backend/agent_http.py`|Agent 后端契约与实现（`backend.py`/`reasonix.py`）、进程与 wire（`runtime.py`/`protocol.py`）、事件日志与 SSE（`events.py`/`stream.py`）、桥接（`bridge.py`），以及组合根与 HTTP 路由|
|`backend/runtime_isolation.py`|每个数据集/会话的 Reasonix 目录与模型凭据隔离|
|`backend/mcp_server.py`|工作台 MCP 能力（业务问题桥接已在 M5-6B 退役）|
|`backend/source_labels.py`|已有标签列接入、人工值保护、停用/恢复|
|`backend/source_label_inference.py`|已有标签画像、24/60 混合抽样、视觉分组、标准草案和确认|
|`backend/batch.py`、`backend/vision.py`|直接模型请求、批处理、视觉观察、缓存和模型尝试记录|
|`backend/model_settings.py`|多服务/Key/模型、文本和视觉独立验证、探针错误分类|
|`backend/worksheet_query.py`、`backend/field_catalog.py`|字段识别、筛选、画像和代表样本|
|`backend/execution_contract.py`、`backend/tagging_rules.py`|任务冻结契约和规则加载|
|`skills/product-tagging/`|商品通识打标 Skill 0.5.0 及模块、案例、校验清单|
|`frontend/src/main.jsx`|当前主要前端交互|
|`frontend/src/beta5.css`|beta.5/beta.6 主要样式|
|`scripts/build_windows.py`|Windows 候选/正式包构建|
|`scripts/repository_audit.py`|源码边界、密钥和禁止路径审计|

### 5.3 数据库主要持久化对象

`Dataset`、`Product`、`Asset`、`DraftColumn`、`Cell`、`Proposal`、`BatchJob`、`Snapshot`、`Message`、`ChatQuestion`、`ChatSession`、`AgentRuntimeSession`、`SourceLabelDraft`、`DraftEvent`、`VisionCache`、`TextRecord`、`TextCache`、`ModelAttempt`、`Audit`。

当前 schema 版本为 8。任何迁移必须：

- 先用 SQLite backup API 做一致备份；
- 可重复启动而不重复迁移；
- 保留旧数据、旧会话和任务证据；
- 回退时恢复“旧程序 + 对应升级前数据备份”，不能只替换程序。

### 5.4 当前 Agent 运行时链路与已退役路径（2026-09-16 现场核对）

两条生产执行链，二者都只经 Agent 后端契约，互不共用进程状态：

```text
UI Agent   → AgentBridge → AgentBackend → ReasonixAdapter → Runtime → Reasonix → MCP → 工作台业务层
Jobs/Worker/Coordinator → AgentBackend → ReasonixAdapter → Runtime → Reasonix → MCP → 工作台业务层
```

已退役、**不得再被当作现行路径**：

| 已退役 | 退役于 | 现状 |
|---|---|---|
| `backend/conversation.py`（网页聊天回合调度） | M5-4 | 模块已删除；`chat_threads`/`ChatQuestion`/`Message` 只作历史查看与 CRUD |
| `harness.ACP` 运行时客户端 | M5-6A（M5-5 先迁走 jobs） | 生产构造调用为 0，类已删除；`harness.py` 只保留启动/配置职责 |
| MCP `ask_business_question` + `TAG_QUESTION_OWNER` + `allow_business_questions` | M5-6B | 工具、开关、生成配置项全部删除；`business_questions.py` 已删除，`ChatQuestion` 历史行保留 |
| 网页 `/api/datasets/{did}/chat*`、`/api/drafts/{did}/runtime` 路由 | M5-3/M5-4 | 未注册；旧命令面已静默 |

仍在位、属于**历史业务数据**而非运行时的面：conversation CRUD、`Message` 历史、`ChatQuestion` 历史、附件、`Message.presentation` 历史字段、history-only viewer、Draft SSE 业务投影、`AgentRuntimeSession`（仅 `chat_threads.destroy` 清理用）。

需要用户交互的问题只走 **Reasonix native question → AgentBridge**；后台 worker/coordinator 不具备交互式提问能力。

### 5.5 M6 Runtime Hardening COMPLETE（2026-09-17）

M6 三个子轮次（M6-1 Process Lifecycle、M6-2 Execution Budget、M6-3 Model/Credential Lifecycle Safety）已全部实现、测试并本地提交，共 5 个提交：`e1a6b6e`（Job Object）→ `cf0e968`（graceful shutdown）→ `139809c`（语义预算）→ `246f1b2`（生产预算注入）→ `3257e50`（模型/凭据快照）。**本轮之后不再新增 runtime milestone（没有 M6-4 / M6-5）。**

1. **Process ownership** — Reasonix 进程树由 Windows Job Object 托管（匿名 job + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），`Popen` 后立即 assign、`stop()` 最后 release；宿主不支持时降级为 `job_unavailable` 事件并把事实暴露在 `diagnostics()['job']`。
2. **Graceful shutdown** — `agent_application.start()/shutdown()` 受 lifespan 调用；关闭时先为快照里**每个**会话启动各自的 close worker，再用一个共享 deadline 统一等待，因此一个不肯关闭的会话不会饿死后面的会话；关闭期间不再接受新会话（`ApplicationStopping` → 503）。
3. **Execution budgets** — first-activity / idle / tool-wait / wall-clock 四层语义预算，按 UI / Worker / Coordinator 显式注入：

   | 角色 | first | idle | tool | wall-clock |
   |---|---|---|---|---|
   | UI Agent（`agent_application`） | 120 | 180 | 360 | 1200 |
   | Worker（`jobs._execute`） | 180 | 300 | 360 | 1200 |
   | Coordinator（`jobs._chat`） | 120 | 240 | 360 | 1200 |

   wall-clock 是绝对上限（活动不延长）；`budget=None` 保持旧的单层行为（generic seam 默认仍为 `None`）；传输层上限 = wall-clock + 60s 宽限，MCP 单次调用仍是它自己那一层的 300s。
4. **Interrupted-turn recovery** — 被打断的回合**不复用**旧 Runtime：先尽力 `session/close`，再 teardown，下次 `send()` 新建 Runtime 并对**同一个** native session 发 `session/resume`（+ 扩展重载）；恢复失败只报 `RuntimeUnavailable`，**绝不**回退 `session/new`。
5. **Stale-event isolation** — interrupt 是 exactly-once（first writer wins，`_turn_cond` 下判定），并立刻关闭 stale-event fence（`accept_peer_output=False`），因此被中断回合的迟到帧不会串进下一轮；显式 cancel 与语义超时走同一条回收路径。
6. **Model / credential snapshot safety** — 创建 Session / Run 时一次性冻结 model + provider + credential；active workload 存在时一切会改变 launch 解析结果的 Settings 变更返回 409 `settings_in_use`；worker 一个 run 内只解析一次凭据；dangling / disabled 凭据一律显式失败、不回退 primary；recovery 复用创建时的 launch environment。另有共享 home 冲突保护：同一 dataset 已有活动会话且冻结配置不同 → 409 `runtime_config_in_use`。

## 6. 版本、构建、安装和 Git 现状

### 6.1 当前现场（2026-09-13 已复核）

|项目|值|
|---|---|
|源码分支|`feature/0.5.0-delivery`|
|源码 HEAD|不要引用本文档写死的哈希：文档自身也是提交，此值必然过期。用 `git log -1 --format=%H` 现场读取，实质锚点是“候选构建提交”一行|
|远端分支|`origin/feature/0.5.0-delivery`，2026-09-13 交接时已推送并与当时本地一致。**2026-09-17 现状：远端停在 `1ceb4ac`，本地领先 13 个提交且未推送**（M1–M6）。推送需用户逐次授权|
|唯一版本源|`VERSION.json` = `0.5.0-beta.7`、schema 8、`pending_acceptance`（现场以 `VERSION.json` 为准）|
|候选构建提交|`2f7bf2b59975ba2cc5693ec346868d470774fe61`（本机安装的就是它；后续文档提交与任何 P0 修复都不在该包内）|
|本机安装 build ID|`0.5.0-beta.6+2f7bf2b59975`|
|本机安装目录|`K:\Bobbytagging\app`|
|本机数据目录|`K:\Bobbytagging\Data`|
|当前正式发布标签|只有历史 `v0.5.0-beta.3`|

候选安装程序：

`releases/0.5.0-beta.6/2f7bf2b59975/格物工作台-0.5.0-beta.6-candidate.2f7bf2b59975-安装程序.exe`

SHA256：

`4a5524d07bcd941085c1676d2630139912ebca6a2f73591e50f75cd37167a8a0`

同一版本目录下还有一个同日废弃构建 `releases/0.5.0-beta.6/2c2b9d56de58/`（17:47 产出，早于当前候选 `2f7bf2b59975`）。它不在台账里，不要误当作当前候选或用于验收。

本机 beta.5 升级前备份：

`K:\Bobbytagging\Data\backups\upgrade-20260913T095143837778Z`

另有一次 beta.6 安装过程备份：

`K:\Bobbytagging\Data\backups\upgrade-20260913T100121205999Z`

旧 beta.5 候选与回退安装程序仍在：

`releases/0.5.0-beta.5/575333ac1b37`

### 6.2 已验证的候选证据

- 后端完整测试：`160 passed`。
- 前端生产构建：1580 modules transformed，通过。
- 仓库审计（候选构建时）：212 个受控文件，密钥和禁止路径命中均为 0。此后新增本文档与 beta.6 反馈文档，当前为 214 个。
- 包内冷启动、前端加载、健康接口和控制台通过。
- 本机 beta.5 → beta.6 覆盖升级成功，当前健康接口为 HTTP 200。
- 当前运行进程报告 `dirty=false`，构建提交与候选台账一致。

这些只证明结构、兼容和安装链路，不证明真实模型的反推语义与对话恢复已经验收。

### 6.2.1 当前源码证据（M6 收口，2026-09-17）

以下数字属于**本地分支源码**，`2f7bf2b...` 安装包内是旧代码，两者不要混用：

- 后端全量测试：**596 passed**（`python -m pytest -q`，真实全量执行）；
- 仓库审计：`{'tracked_files': 257, 'forbidden_paths': [], 'secret_hits': [], 'sample_hits': []}`（M6 收口时的受控文件数；新增本文档之后的清单文件会使其变成 258，命中项仍须全为空）；
- M6 专项套件：`tests/test_agent_runtime.py`（Job Object）、`tests/test_agent_http.py`（graceful shutdown）、`tests/test_execution_budget.py` + `tests/test_budget_policy.py`（预算）、`tests/test_model_credential_lifecycle.py`（模型/凭据快照）；
- 前端本轮未改动，沿用最近一次全绿的生产构建；如最终交付前有前端改动，须重跑 `npm run build --prefix frontend`。

### 6.3 发布纪律

- beta.6 是内部候选，状态仍为 `pending_acceptance`；不要创建正式 tag/Release 或称为正式发布。
- 候选构建绑定 `2f7bf2b...`。后续代码修复不能宣称已进入该安装包；必须重新测试并生成新构建记录。
- 为避免同名候选混淆，建议两项 P0 修复完成后进入下一个 prerelease 序号，而不是覆盖现有 beta.6 产物；最终以 `RELEASE_GOVERNANCE.md` 和用户决定为准。
- 推送分支已由用户在 2026-09-13 交接时明确授权并完成；此后新增提交是否推送仍须逐次授权。推送不等于验收通过：不得创建 beta.6 正式 tag/Release，不得标记 `accepted`。

## 7. 当前正式数据快照

当前服务只读核对到 1 个数据集：

- ID：`e364ea99448d4156b941bf08d02b8f18`
- 名称：`Product-Clothing,Shoes&Jewelry-US-2026.05-08-按父体ASIN合并`
- 工作表：`Product-US-202605-202608`
- 商品：310 行
- 导入图片资产：310 件有图
- 原始文件：`K:\Bobbytagging\Data\originals\e364ea99448d4156b941bf08d02b8f18.xlsx`

“上衣样式”真实画像：

- 17 行已有人工值，293 行空白，空白率约 94.52%；
- 8 个原始值：`U领运动` 5、`U领吊带` 4、`三角杯` 2、`圆环三角杯` 2、`V领交叉前襟` 1、`V领吊带` 1、`抹胸吊带` 1、`斜肩上衣` 1；
- 每个已有值均有对应图片；293 个未标商品也都有图；
- 原列已经接入，正式标签列 ID 为 `f00f9f35a9c74549adf0bf9170c32420`，revision 1；
- 当前人工定义为“从整体造型结构，主要是肩，领，胸杯的造型来区分。”；
- 该定义仍有轴混合问题：领型、杯型、前襟、肩部结构和风格词共处一列，值不一定互斥，正是需要真实图片反推和人工定边界的案例。

不得在排错或验收中自动改写这 17 个值或重打 293 行。所有真实模型复测应先复用已有 `ModelAttempt`/草案和隔离副本，只有用户确认才触碰正式标签标准。

## 8. 已完成的里程碑

### 8.1 初版至 beta.3

- 用真实 10 行、70 字段、10 张 WPS 内嵌图文件完成导入、查看、三类标签、导出闭环。
- 从卡片结果改造成“左对话 + 右统一工作表 + 持续草稿 + 用户主动保存版本”。
- 建立 Reasonix coordinator + `product_workbench` MCP + 直接 batch 的职责分层。
- 落实图片资产 ID 证据、原始值/标准值、转换历史、人工优先和三 Sheet Excel 导出。
- 修复 beta.2 安装目录误判；beta.3 采用有效旧版原目录覆盖升级。
- beta.3 历史客户试用 Pre-release 已发布，tag 为 `v0.5.0-beta.3`。

### 8.2 beta.4 / Skill 0.5.0

- 商品通识打标 Skill 模块化：工作模式、证据、实体范围、校准、成本、属性维度、品类和产品形态。
- 只加载命中模块，冻结 Skill/模块哈希和运行配置。
- 视觉校准按组、可扩 24/60，任务末集中整理定义缺口，避免执行中不断弹拆列卡。
- 默认单主图，其他图片策略必须显式确认。

### 8.3 beta.5

- 项目创建/选择和工作表组织。
- 同一工作表多个网页对话，新建、切换、重命名、回收站、恢复和彻底删除。
- 对话图片上传、粘贴、拖入、稳定编号和商品绑定。
- 多 Provider、多具名 Key、文本/视觉模型分别选择和任务冻结。
- 已有人工标签原列接入、非空保护、停用/恢复和原位置导出。
- Ask/问题卡、错误展示和提案去重进行了扩展，但 beta.6 实测说明问题桥接和超时仍未真正闭环。

### 8.4 beta.6

- 已有标签全量程序画像。
- 反推按钮、证据列选择、自动读取已有标签、已标+未标混合抽样、24/60 扩样估算。
- 标签标准草案、问题、覆盖确认、原值保护及已接入列修订。
- 网页对话、原生 Reasonix 会话、后台任务三层持久化/隔离基础。
- 模型文本/视觉独立验证，视觉探针从 beta.5 的 1×1 改为 512×512 JPEG，并区分常见错误类别。
- 候选包构建、包内冒烟和本机覆盖升级。

## 9. beta.6 P0 缺陷（**已于 beta.6 修复**；以下保留为问题记录）

两项均已修复并留下验证记录：见 `docs/BETA6_P0_VERIFICATION.md`（F01 先解析后判截断、图片字段规范化与映射、复用已存响应、程序基线；F02 分层超时/归因/进度/问题桥接）。本节以下文字是当时的原始问题陈述，不要据此判断当前代码状态。

其中 F02 的第 3、4 条（业务提问及时落库、回答恢复原回合）在后续 M5-6B 被更强的方案取代：**该能力整体退役**——MCP `ask_business_question` 已不存在，交互统一走 Reasonix native question → AgentBridge。

完整证据见 `docs/BETA6_TEST_FEEDBACK-20260913.md`。

### F01：反推有可用结果，却被整轮判失败

复现：来源列“上衣样式”，首轮 24 件，模型 `CS/z-ai/glm-5.3-flash`，随后点击继续/重试。

已确认事实：

- 模型工作了，不是“模型完全没响应”；
- 两次都是文字请求，约 81.5 秒和 87.4 秒；
- 两次都达到 `max_tokens=8192`，`finish_reason=length`；
- 第二次内容实际可解析，且包含总定义、8 个已有值规则、4 个冲突和未知策略；
- `backend/batch.py` 当前在 JSON 解析/结构校验前看到 `finish_reason=length` 就抛“输出截断”；
- 视觉请求为 0。用户选择了 Excel 原始“图片”列，而执行层只把内部特殊字段 `__image__` 当作视觉输入；
- 失败文案错误地声称会复用“已成功保存的视觉组合”，但本轮没有视觉调用。

应实现：

1. 先解析 JSON 和做最小 schema 校验，再判断截断风险；完整合法结果进入审核，并标明可能截断。
2. 若只有部分结构可用，保存已完成区块，只请求缺失区块；不要原样重发整个 8192-token 请求。
3. 首先从 `ModelAttempt` 恢复第二次已有响应，避免再次付费。
4. 模型完全不可用时，仍基于程序画像生成“程序基线草案”；明确标注未经过模型/视觉核验，不伪装成完整结论。
5. 将 raw “图片”列与内部图片资产消歧：UI 只保留一个明确的“商品主图（已导入图片资产）”，或在提交时稳定映射到 `__image__`。
6. 任务卡按真实阶段展示文字请求、视觉请求、缓存/恢复来源、最近活动和错误。

必要回归：

- `finish_reason=length` + 完整合法 JSON 应成功并带风险标志；
- 截断但可恢复的 JSON 只补缺失字段；
- 复用已有 `ModelAttempt` 时请求数为 0；
- 选择 raw “图片”能正确触发真实图片资产，或 UI 不再允许误选；
- 无视觉时不得展示视觉复用文案；
- 模型连续失败仍可生成标明来源的程序基线，不允许“无结果死路”。

### F02：新建对话约 11 分钟零反馈后失败

复现输入：`判断上衣样式，我人工打了一些标签可以结合图片做参考`。

已确认事实：

- 18:17:01 开始，约 18:27:55 失败，总耗时约 10 分 54 秒；
- 18:20:47 时仍为 running，助手正文 0 字、可见进度 0 条；
- Reasonix 进程一直存活，前两分多钟至少有 8 次模型调用，最近上下文约 22355 tokens；
- `ask_business_question` 的 `product_workbench` MCP 调用等待满 5 分钟后超时；
- 同轮还尝试调用权限层未开放的 `run_skill`，最终界面只突出 `tool_permission_denied`；
- `backend/harness.py` 对 `session/prompt` 使用 `timeout=None`，外层无总时限；
- 内部活动没有转成用户可见阶段，因此看起来像“模型没工作”。

应实现：

1. 建立首事件、静默和总时限：数秒内必须显示已接收/理解工作表；静默阈值提示仍在工作或可能卡住；总时限后中止当前等待但保留可恢复状态。
2. 将模型调用数、最近活动时间、当前阶段、当前工具、等待用户问题等作为 SSE 可见事件。
3. `ask_business_question` 必须及时落库并显示网页问题卡；不能让 MCP 服务端同步阻塞 5 分钟等待用户回答。
4. 用户回答通过独立持久化通道恢复原回合，或明确结束旧回合后从下一步继续；不能用无限占用 HTTP/ACP 请求模拟交互。
5. 工具权限错误、业务问题超时、Provider 错误要按时间和因果选择主错误；不能让后发生的 `run_skill` 遮盖实际造成等待的超时。
6. coordinator 指令和工具清单必须一致：提示里不要诱导调用未开放的 `run_skill`；需要 Skill 时使用已开放的 `route_tagging_skill` / `read_tagging_rules`。
7. 停止回复应在限定时间内结束等待、关闭当前问题、释放数据集 busy；不取消已经确认的后台批量任务。

必要回归：

- 新建对话首次简单请求在数秒内出现可见进度；
- 问题卡出现后无 5 分钟 MCP 阻塞；
- 用户直接文字回答、跳过、停止、刷新恢复均可继续；
- 模型/工具活动期间最近活动时间持续更新；
- 总超时后保留网页消息、原生 session ID 和已完成工具结果；
- A→B→A 模型切换与多个网页对话不串消息、问题和任务；
- 禁止工具不会被反复调用，也不会覆盖更早、更主要的超时原因。

## 10. 后台 CLI 对话管理的目标机制（**历史方案**）

> 状态：本节写于 beta.6 时期，描述的是当时存在的网页聊天运行时。该运行时已在 M5-4 退役，交互式对话改由 Agent 面板 + `AgentBridge` 承载，因此下面的状态机与持久身份设计只作历史参考；其中已落地的部分见 §5.4。

这是两项失败共同暴露的架构重点。建议把状态机明确成：

```text
网页消息 accepted
→ 立即持久化并发 SSE acknowledgement
→ 选择/创建 AgentRuntimeSession
→ 注入受控上下文（数据摘要、已确认标准、近期消息、图片索引）
→ Reasonix 分阶段运行
   ├─ 读取/提出建议：事件持续可见
   ├─ 需要业务问题：持久化问题并结束或挂起可恢复步骤
   ├─ 需要后台任务：创建独立 BatchJob 后立即返回
   └─ 完成：持久化正文、提案和 session 位置
→ 超时/停止/失败：保存 checkpoint，释放锁，提供继续入口
```

建议持久身份：

- `ChatSession`：网页对话，长期稳定；
- `AgentRuntimeSession`：按网页对话 + 模型/Key配置修订 + coordinator/worker角色创建，可替换；
- `Message`/`ChatQuestion`：用户可见历史和待回答业务状态；
- `BatchJob`：脱离聊天生命周期的执行；
- `ModelAttempt`：每个付费请求的原始可恢复证据。

切换模型：保留 `ChatSession`，新建 `AgentRuntimeSession`，只注入有限近期消息、确认过的业务状态和摘要。切回旧模型时可以恢复对应原生会话；若原生会话失效，则重建并注入同一受控上下文，不得告诉用户“整个对话丢失”。

后台新建“打标任务”时不要新建用户对话框；任务只记录来源对话/提案作为审计关系。只有用户明确点击“新建对话”才创建新的 `ChatSession`。

## 11. 尚未完成的产品目标

按当前真实阻塞排序：

### P0：先恢复可信和可完成性（**已完成**：F01/F02 于 beta.6 修复，记录见 `docs/BETA6_P0_VERIFICATION.md`）

- 修复 F01：截断结果解析/恢复、局部补全、程序基线和图片字段映射。
- 修复 F02：对话可见进度、问题桥接、分层超时、停止/恢复和主错误归因。
- 用“上衣样式”真实 24 件重跑，但先离线复用已有结果；确认真实图片确实进入视觉模型。
- 保证任何反推路径都有可审核结果或明确的最小程序草案，不留下只有“重试”按钮的死路。

### P1：完成 beta.6 原验收门禁

- 两个真实模型做文本、视觉、对话和 5 件小批量矩阵；各状态独立展示。
- 人工核对 24 张代表图、8 个已有值、未标新候选、冲突和边界。
- 验证已接入原列修订同一标准、不复制列、不覆盖 17 个人工值。
- 人工演练升级备份回退。
- 干净 Windows 10/11 安装、升级、卸载不删数据。
- 修复后重新构建候选、做包内冒烟、本机覆盖升级，再交用户测试。

### P2：打标质量和成本

- 建立可复用的“主体/实体 → 客观视觉观察 → 标签映射”快照层，减少同图重复视觉请求。
- 建立泳衣多主体、配件、颜色、印花边界案例集，记录旧值、期望值、规则和未知原因。
- 成本仪表盘和执行前预算/请求量上限。
- 对话上下文压缩，避免简单请求膨胀到两万以上 token 和多轮工具探索。
- 任务末聚合定义缺口，进一步降低对话卡片噪音。
- 评估 WPS 原生 `DISPIMG` 回写；当前导出图片是可用的浮动图片，不能假装已完全恢复 WPS 原公式语义。

### 后续硬化 backlog（**已由 M6 完成**，2026-09-17 更新）

以下三项曾是 M5 之后的主要硬化目标，现已全部实现、测试并本地提交，见 §5.5：

1. ~~**Reasonix process lifecycle**~~ → 已完成（M6-1）：Job Object 托管进程树 + graceful shutdown。
2. ~~**Execution budget**~~ → 已完成（M6-2）：first-activity / idle / tool / wall-clock 四层预算 + 按角色显式注入 + 中断回收与同会话 resume。
3. ~~**Model / credential lifecycle safety**~~ → 已完成（M6-3）：创建时冻结 model/provider/credential + active workload 期间 Settings 变更锁 + dangling/disabled 显式失败。

### Deferred backlog（**NON-BLOCKING FOR DELIVERY**，只记录不实现）

以下条目已明确评估为不阻塞交付，**本轮及后续 Real Acceptance 阶段都不要顺手清理或实现**：

**PRODUCT BACKLOG**
- AgentPanel 不传 model 时回落到 `harness.DEFAULT_MODEL`，与 Settings 里的 `default_model` 不一致。属产品行为问题，不是 lifecycle safety。

**KNOWN LIMITATION**
- 删除 credential 后，旧 dataset home 的 `.env` 可能残留该 secret，直到下一次 `environment()` 重写；已验证会被重写覆盖，不做全盘扫描清理器。
- durable credential identity across process restart 当前未实现（进程重启后旧会话不可在进程内恢复，故暂不需要持久化 credential id）。

**DEFERRED CLEANUP**
- `harness.py` naming（模块已只负责启动/配置，名字仍是历史名）。
- legacy presentation residue（`Message.presentation` 历史字段）。
- `protocol.py` 未被使用的 helper。
- `AgentRuntimeSession` 历史残留（仅 `chat_threads.destroy` 清理用）。
- `TAG_CONVERSATION_ID` / `view_chat_image` 残差。
- 其它 M5 已归档 residue。

### 发布/协作待办

- 本地分支已于 2026-09-13 交接时推送至 `origin/feature/0.5.0-delivery`；此后新增提交是否推送仍须用户逐次授权（当前本地领先远端 13 个提交）。
- beta.6 真实验收失败，不能标记 `accepted`，不能创建 beta.6 正式 tag/Release。
- 两项 P0 修复完成后更新 `CHANGELOG.md`、`RELEASE_LEDGER.json`、交付台账、验收记录和安装包哈希；不要回写旧构建证据。
- 真实验收（Phase A/B）与候选交付验证（全新安装/升级/回退）已完成，记录见 `docs/ACCEPTANCE-0.5.0-beta.7.md`；下一步只剩**正式数据上的语义确认与生产切换**（需用户授权），在此之前不建 tag/Release。

## 12. 接力实施建议（**历史**：阶段 A–C 已完成）

> 状态：以下分阶段建议写于 beta.6 修复前。阶段 A–C 已执行并留存验证记录（`docs/BETA6_P0_VERIFICATION.md`）；阶段 D 的构建与真实验收**尚未执行**，仍是 beta.6 的剩余门禁（见 §11）。

### 阶段 A：只读复现与保护现有证据

1. 读取 `docs/BETA6_TEST_FEEDBACK-20260913.md`。
2. 在正式库中定位对应 `SourceLabelDraft`、`BatchJob` 和两条 `ModelAttempt`，先导出/断言，不执行新模型请求。
3. 建立测试夹具，证明第二次响应可解析。
4. 定位 F02 的 `ChatSession`、`AgentRuntimeSession`、消息、问题和诊断时间线；不得删除原会话缓存。

### 阶段 B：修复 F01

1. 把模型响应处理拆为：提取内容 → 尝试 JSON 恢复 → schema 校验 → 判断是否缺失 → 决定接受/补全。
2. 为 source-label inference 建立专用紧凑输出契约，避免复用对普通批量标签过重的通用请求。
3. 增加 `ModelAttempt` 重放入口或内部恢复函数。
4. 统一 UI 图片选择字段与 `__image__`；对旧草案做兼容映射。
5. 实现程序基线和来源徽标。

### 阶段 C：修复 F02

1. 先补可见进度事件和最近活动时间，再改超时；便于实测定位。
2. 将业务问题改成“持久化后立即返回给网页”，不要同步等待用户 5 分钟。
3. 给 Reasonix RPC 建首事件/静默/总时限，并实现可恢复 checkpoint。
4. 校准 allowed tools、system prompt 和错误优先级。
5. 验证 stop、refresh、模型切换和多对话隔离。

### 阶段 D：交付

1. 全量 pytest、前端生产构建、仓库审计。
2. 隔离目录真实模型验收；明确会消耗额度，复用历史响应的测试不应付费。
3. 构建新候选并记录 commit、build ID、SHA256、包内文件清单。
4. 包内冷启动和本机覆盖升级；保留升级前一致备份。
5. 交用户测试前给出安装位置、当前运行 build ID、已修复项、仍未验收项和回退路径。

## 13. 验证命令

在 PowerShell、仓库根目录运行：

```powershell
git status --short --branch
git log --oneline -10
Get-Content VERSION.json -Raw
.venv\Scripts\python.exe -m pytest -q
npm run build --prefix frontend
.venv\Scripts\python.exe scripts/repository_audit.py
Invoke-RestMethod http://127.0.0.1:8765/api/health | ConvertTo-Json -Depth 8
```

构建候选前工作区必须干净，并按 `docs/WINDOWS_BUILD.md` 准备锁定输入：

```powershell
.venv\Scripts\python.exe scripts/fetch_build_inputs.py
.venv\Scripts\python.exe scripts/build_windows.py --candidate
```

真实模型验收会消耗额度，必须使用隔离数据目录和已授权的本机凭据。不要把客户文件、正式数据库、缓存、会话或 Key 打入源码 ZIP/安装包。

M6 收口后的专项回归（离线、零付费）：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_agent_runtime.py tests/test_agent_http.py `
  tests/test_execution_budget.py tests/test_budget_policy.py `
  tests/test_model_credential_lifecycle.py -q
```

## 14. 重要文档索引

- `README.md`：当前产品操作与总体能力。
- `docs/REAL_ACCEPTANCE_CHECKLIST.md`：真实验收清单（Phase A/B 已按此执行，结果见 beta.7 验收记录）。
- `docs/DELIVERY_STATUS.md`：交付门禁总台账。
- `docs/ACCEPTANCE-0.5.0-beta.7.md`：**当前候选**的验收记录（真实模型 Phase A/B、候选安装/升级/回退与 packaging 冒烟）。
- `docs/ACCEPTANCE-0.5.0-beta.6.md`：上一版候选的自动化、安装和剩余人工验收。
- `docs/BETA6_P0_VERIFICATION.md`：beta.6 两个 P0 的修复与验证记录。
- `docs/BETA6_TEST_FEEDBACK-20260913.md`：本轮两个 P0 真实失败。
- `RELEASE_LEDGER.json`：版本、提交、候选安装包和 SHA256。
- `docs/RELEASE_GOVERNANCE.md`：候选、正式版本和冻结规则。
- `docs/WINDOWS_BUILD.md`、`docs/UPGRADE_ROLLBACK.md`：构建、升级和回退。
- `docs/PRODUCT_TAGGING_SKILL_0.5.0.md`：Skill 架构和运行链路。
- `skills/product-tagging/SKILL.md`：当前运行 Skill 入口。
- `docs/KNOWN_ISSUES.md`、`docs/VISION-0.6.0-ROADMAP.md`：历史残差和中期方向。
- `本机工作台入口与操作规则.md`：本机唯一入口和操作边界。

## 15. 禁止误操作清单

- 不在正式 310 行数据上自动重打或修改 17 个人工值。
- 不删除 `K:\Bobbytagging\Data`、SQLite WAL、Reasonix 会话或升级备份。
- 不把当前安装的 `2f7bf2b...` 描述为包含后续源码/文档修复。
- 不把本地未推送的 Agent 运行时迁移与 M6 硬化（M1–M6，13 个提交）描述为已发布或已进入安装包。
- 不以自动化测试通过（当前 596 passed）代替真实模型语义验收。
- 不把已经评估为 NON-BLOCKING 的 deferred backlog（见 §11）当成交付 blocker，也不在验收阶段顺手清理。
- 不因 `finish_reason=length` 未解析内容就直接丢弃已付费响应。
- 不把 raw Excel “图片”列和内部 `__image__` 资产当成两个可由用户自行猜测的概念。
- 不用无限 `session/prompt` 等待承载需要用户回答的交互。
- 不允许 Reasonix 退回 `bash` 或任意文件系统搜索；工作台数据只能经受控 MCP/API。
- 不静默改名、合并、纠错或扩充人工标签。
- 不把完整 Key 写入文档、日志、Git、任务证据或错误详情。
- 不创建 tag/Release、不标记 `accepted`，除非用户明确授权且验收门禁已经满足；推送分支需用户逐次授权（本次交接已授权）。

## 16. 接力完成标准

只有同时满足以下条件，才可以向用户说“这一轮修复完成，可以测试”：

- F01 的历史有效响应无需付费即可恢复为审核草案；图片字段选择能触发真实视觉；失败时也有来源明确的基线结果。
- F02 的简单请求能快速看到进度，问题卡不阻塞 5 分钟，停止/超时后可恢复且锁已释放。
- 自动化、前端构建、仓库审计全部通过。
- 真实模型小样本验证分别记录文本/视觉/对话/批量结果，不隐藏失败。
- 新候选绑定唯一 commit 和 SHA256，包内冒烟与本机覆盖升级通过。
- 交付台账、验收、变更说明、回退点和未完成门禁同步更新。

在此之前，应使用“已记录”“已定位”“代码已修复待构建”“候选已构建待真实验收”等精确状态，不使用笼统的“已经好了”。

M6 收口后的下一阶段验收标准不再沿用本节：真实验收已按 `docs/REAL_ACCEPTANCE_CHECKLIST.md` 执行完毕，结果与剩余门禁见 `docs/ACCEPTANCE-0.5.0-beta.7.md`。
