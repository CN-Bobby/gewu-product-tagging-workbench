# 格物工作台项目总进度

快照时间：2026-10-09（Asia/Shanghai）

> 当前运行治理状态更新于 2026-10-13：新功能日常人工测试统一在 DEV，独立 TEST 服务已停止并保留数据。此前的 TEST 候选流程记录为历史方案，详见 [DEV 唯一日常人工测试环境](DEV_ONLY_DAILY_TESTING.md)。

## 2026-10-12 便携分发与在线更新流程（实施中）

- **当前状态：** DEV → 便携候选 → TEST 人工验收 → PROD 提升与 GitHub Release 流程正在接入；没有新的候选 ZIP、人工验收或 PROD 提升记录，本节不代表已完成交付。
- **真实 DEV：** Run Center 指向 `C:\Users\Administrator\.codex\worktrees\voc-project-file-link\数据打标项目`，分支 `codex/voc-project-file-link`，基线 HEAD `267a3dff360cfd67724f8e920ad8091a4ad076b6`；本次复核时有 68 个 tracked 文件修改和 13 个未跟踪文件，须分类保留后才能形成干净候选提交。聊天当前打开的 `1b41` worktree 是干净 detached 基线，不是 DEV。
- **版本口径：** beta.8 清单实际标记 `build_mode=dev`、`release=false`、不可分发，且没有对应人工验收报告；现将其登记为被 beta.9 取代的开发构建。`VERSION.json` 与 RELEASE_LEDGER 已推进 beta.9 / `pending_acceptance`，候选 commit 尚未确定。
- **已接入本地改动：** `build_windows.py --portable` 输出版本化候选目录、便携 ZIP 和 SHA-256 侧车；人工验收清单绑定候选报告、app manifest、ZIP 和侧车哈希；TEST/PROD Run Center 门槛与提升凭证脚本正在完善。PowerShell 静态解析及 `git diff --check` 通过；本轮尚未构建候选或执行完整测试。
- **在线更新口径：** 客户端读取 GitHub Releases，不同步源码分支。接受提交进入源码仓库 `main` 后，将人工验收的同一 ZIP/侧车上传至二进制 Release 仓库，并在说明中记录源码 commit。默认配置 `CN-Bobby/gewu-workbench-releases` 当前不存在，需要在候选完成后确认/建立可公开下载的二进制仓库。
- **尚需完成：** 对 68 项 dirty 变更分类及重复 Woot 补丁对照；完成分支本地提交和固定候选工作树同步；完善 TEST/PROD 提升脚本和按原 ZIP 发布脚本；离线测试、构建、浏览器回归；人工验收并产生可追溯报告；按授权提升 PROD；配置 Release 仓库并验证客户端检查更新与资产哈希。

## 本地开发与测试环境治理

- **状态：环境治理实现完成，人工业务验收仍待用户执行。** 日常开发入口、冻结 TEST 构建、数据重置、回退、离线自动门禁、项目文件树浏览器回归和独立人工清单已形成固定流程，说明见 [`LOCAL_RUN_CENTER.md`](LOCAL_RUN_CENTER.md) 与 [`LOCAL_TEST_ACCEPTANCE.md`](LOCAL_TEST_ACCEPTANCE.md)。DEV 与 TEST 每次桌面启动都显式选择 offline（默认）或单进程 `live_validation`，取消/关闭对话框会中止启动。两种模式都使用隔离业务数据，且不恢复旧任务；自动回归与候选 `StartAfter` 始终 offline。TEST 夹具位于 `K:\GeWuFixtures\project-library-v1`，TEST 报告位于 `K:\GeWuTest\Reports`。
- 实现位于隔离 worktree `C:\Users\Administrator\.codex\worktrees\local-test-governance\数据打标项目`，分支 `codex/local-testing-governance`。主工作目录 `D:\数据打标项目` 的既有未提交修改不属于本任务，未覆盖、清理或纳入提交。
- 实现工作树提交前的离线全量门禁报告：`K:\GeWuTest\Reports\offline-20261008-235316-2964ba1929ac.json`。后端 **822 passed / 8 skipped**，前端 **141 passed**，Vite 构建通过，外部请求数为 0。TEST 冻结脚本会从指定的干净提交重新运行这套门禁并另外记录候选身份。
- 从 `K:\Bobbytagging\Data-Test` 一次性导入模型与 SellerSprite 凭证至当前 Windows 用户 DPAPI vault，来源文件保留且未做密钥验证；DEV/TEST 状态为模型已配置、SellerSprite 已配置、Sorftime 未配置。提供方非秘密设置分别保存于每个环境的 Run Center 目录。旧目录和历史备份中的凭证只盘点、不自动删除。
- 正式数据快照 `K:\Bobbytagging\Backups\pre-run-center-20261008` 已核验，553 个文件、19 个 SQLite 数据库快照、8 个凭证/日志副本排除；manifest SHA-256 `f2ddae84a0ba2618b57c3b3a4f086012c1eef5c32baf98fbed0fcdc4bf09e4f6`。生产数据源未迁移或重置。
- 正式数据入口继续识别现有 beta.8 候选，状态 `pending_acceptance`；`VERSION.json`、`RELEASE_LEDGER.json` 未改变。TEST 候选和自动化报告均不代表用户的人工验收或客户发布。

仓库：`D:\数据打标项目`
用途：项目内新对话的接力入口。无需用户转发旧对话；先读本文件，再按 `AGENTS.md` 对需要继续的部分做实时复核。

> **事实口径：**“当前核实”来自本次 Git、健康接口和仓库文档检查；“历史记录”是旧交接或旧验收报告里的事实；“待验收”表示代码或设计已有记录，但没有当前验收证据。版本、部署、验收、分支和运行进程是不同事实，不能互相代替。

## 2026-10-08 项目文件树与预览

- **状态：功能实现，离线回归与隔离数据副本浏览器检查通过；仍是本地开发增量，不是客户候选。** 版本 `0.5.0-beta.7` 未递增，开发构建 ID `0.5.0-beta.7+38821dde910d`，分支 `feature/0.5.0-delivery`，HEAD `38821dde910d0082ed9af8ceedc818ee7461c0e1`，运行状态 `dirty=true`。
- 主导航保留，左侧加入可折叠项目树和新建项目；固定四类成品文件夹提供项目内搜索、时间排序、历史文件版本、Excel 预览与下载。新增 `GET /api/projects/{wid}/library` 和项目/文件 ID 绑定的预览接口。过程文件不进入文件树；旧 `/files` 入口和物理目录保留。
- Excel 预览支持工作表切换、当前表搜索、50 行分页、公式缓存值、浮动图片/WPS 图片；HTML 报告在无脚本权限的 `srcDoc` iframe 中展示。损坏/缺失/哈希失败时给出预览错误，下载仍按项目归属和 SHA-256 校验。历史补齐脚本默认只读核对，只有显式 `--apply` 才复制并登记，要求写出外置核验报告；不移动或删除来源文件。
- 自动验证：`tests/test_project_library.py tests/test_project_library_backfill.py` 为 **11 passed**；前端 **148 passed**；`npm run build` 成功（Vite 提示主 JS chunk 513.29 kB，属于体积警告）。`git diff --check` 通过。
- 浏览器检查绑定到新建的隔离数据副本 `builds/dev-project-library-20261008/storage`，未改源副本或生产数据。狗狗项目页显示 5 个成品（原始数据 1、Data X 2、评论 1、VOC 1）；Data-X V1/V2 均显示，V2 表格首屏 50 行并加载图片，可切换 `AI_Detail`、按 ASIN 搜索到 1 行，文件搜索与最新/最早排序可见。DOG VOC HTML 在 `about:srcdoc` 中渲染。泳衣项目显示原始数据 1、Data X 0、评论 2、VOC 1；空的 Data X 文件夹未产生虚构结果。旧评论表（6,410 字节、仅表头）显示“工作簿中没有可预览的数据行”，下载入口保留；修复版工作簿仍可预览。网页创建的 `项目树验收 2026-10-08` 仅写入此隔离副本，四类文件夹均为空。档案样本页验证所属狗狗项目高亮。
- 本轮文件 SHA-256、项目/页面证据、隔离路径和构建信息见 [`builds/dev-project-library-20261008/evidence.md`](../builds/dev-project-library-20261008/evidence.md)。狗狗 Data-X V1：`3ac024e9a8144e2dc6d8623da977ef04cfb8dd7d6336fc1c220cd6ffd2ab7854`；V2：`3c284aee2d181d1602f4ae92ac9bbc833f08268c36c6430b11b471792b662d6d`。泳衣 Data X 当前为空。
- 未启动付费采集或模型任务。源数据目录未补齐；只用单元测试验证历史脚本默认只读、显式补齐可重复执行且不产生重复副本。隔离测试服务在验证后停止；隔离数据副本保留以便复核。

## 2026-10-02 Amazon VOC 评论闭环与 Reviews 一键转入

- **状态：功能已实现并通过本轮自动回归，保存在本地开发分支，未推送。** 当前分支 `feature/0.5.0-delivery`；客户版本仍是 `0.5.0-beta.7`，本次没有递增版本或创建候选。
- Amazon VOC 工作台使用单个评论 Excel 作为分析输入，复用总设置中的文本模型身份；评论采集页对人工核对过的可下载任务提供“确认并启动 VOC”，使用采集结果 Excel 建立 VOC 任务，并跳转到该精确任务。来源任务与工作簿 SHA-256 做幂等关联；重复提交复用原任务，工作簿改变时拒绝复用。部分失败任务明确提示只分析成功导出的评论。
- 自动验证：Phase 2 独立确定性测试 15 项后端 + 7 项前端通过；Reviews、VOC 与桥接合并回归 24 项通过；前端全量 138 项通过，Vite 构建通过。测试未启动真实 VOC 模型调用。
- 本地可检查页面：`http://127.0.0.1:49720/#/amazon-reviews`，使用隔离目录 `builds/amazon-voc-manual-review/storage`。此目录当前没有评论任务，故列表为空；完成并选中评论任务后才会显示推送按钮。隔离 provider 配置未包含 API 凭据，不会因预览触发付费调用。
- 本次相关实现、文档和测试作为一个本地提交保存；不推送。Amazon 页面使用的导航路由常量一并保存；此前已有的 Windows 构建脚本、寻龙诀样式及 `docs/CODEX_HANDOFF.md` 仍留在工作区，不属于本次提交。提交编号以当前 `git log -1` 为准。

## 2026-09-30 寻龙诀完整实测续验

- **状态：本轮有一条新工作台扫描闭环通过、一条独立源代码对照验收通过，另有一条长跑部分完成；4 个机会结论附语义条件。** 未修改的寻龙诀 v4.2.0-beta.3 原版基准保持有效。完整记录见 [`LOOP_STATUS.md`](../LOOP_STATUS.md) 和 `builds/xlj-parity/US-21551230011-20260929/comparison-summary-final-v3.json`。
- 工作台 Run `fab9531e-5364-4d18-9916-b60d770e946c`：7 个节点、6 条边、4 个机会、2 个淘汰、17 次 Sorftime 调用；`completion_ok=true`。HTML 报告及 SQLite 快照在 `builds/xlj-parity/US-21551230011-20260929/workbench-live-final-runtime-20260930/`。
- 新候选 Run `d87a1136-7d0e-4cf0-bd4d-a06407a0cd4c`（US / Irons & Steamers / Node `510240`）由隔离工作台创建并完整完成：10 个节点、24/24 jobs 成功、4 个机会、3 个淘汰、1 个体量剪枝、0 个暂停；`ALL_DONE` 且 `completion_ok=true`。本轮没有调用原版源码，故这是工作台闭环验证，不是双跑对照。最终 HTML 报告 API 返回 HTTP 200，正文绑定 Run ID；桌面副本 35,940 字节，SHA-256 `33211F3F58125F51DE5365205E9F97CC6CB94CB4FC22373B988037BDCFC64D0C`。
- US / Kitchen & Dining / Node `284507` 的 Run `2904856e-3882-4e7c-ad90-8d38e997db3b` 是部分完成记录：288 个节点、126 个机会、63 个淘汰、51 个体量剪枝、2 个短历史、4 个暂停；恢复后 4 个数据质量异常仍重复出现，最终 `PARTIAL_DONE`、`completion_ok=false`。它保留为部分证据，不计作验收成功。
- 原版 29 条记录及工作台 17 条实采记录分别经最终工作台源码离线回放；都到 `ALL_DONE`，没有缺少、错参或未消费请求；runs/nodes/edges/observations/decisions/jobs 六张表差异均为 0。v3 比较器的全部工程门禁通过，原版报告契约通过。
- Amazon US 根类目页与 6 个子类目页均为 HTTP 200；根页 6 个子 Node ID 与 Sorftime 根树完全匹配，4 个机会页没有显示额外的下级类目链接。Sorftime 实时 `tools/list` 说明 `category_tree` 查询指定节点的子类目，但没有定义纯文本 `No data available.` 必然等于空树。故 Amazon 页面提供独立佐证，机会结论仍是条件性解释。
- 软超时专项 6 项通过，包含 MCP HTTP 响应钩子、纯文本软超时解析、错误阶段及状态记录和脱敏；本轮真实调用没有遇到软超时。全后端 788 passed / 2 skipped，前端 128 passed，生产构建通过。隔离工作台留在 `http://127.0.0.1:8901/#/xunlongjue`，绑定源码 SHA `2ec5e6d82e2b56164c3693c44edd4c213d840b82322c1eb13199f633793e222a`、运行时 `dirty=true`；未改客户版本、未推送。

## 2026-09-29 Amazon 评论模块本地检查点

- **状态：功能实现及记录内的自动化、美国站真实接口验证已完成；临时页面服务当前离线。** 评论模块已接入工作台主导航。代码、接口和数据契约见 [`docs/AMAZON_REVIEWS.md`](AMAZON_REVIEWS.md)；本记录与模块代码包含在本地检查点提交 `feat(amazon-reviews): add review collection workbench module` 中。当前分支 HEAD 已包含该提交及后续开发提交；不推送、不变更 `0.5.0-beta.7`。
- 自动化既有结果：评论模块 9 项通过；全后端 782 项通过、2 项跳过；前端 128 项通过、构建通过。本次保存进度时没有重跑测试。
- 美国站真实验证：Node 与商品标识已从公开文档中移除；4 次评论调用返回 256 条原始记录，去重后 128 条，标题和正文译文覆盖 128/128；工作簿含 3 张表、128 条评论行和 14 个源字段。恢复验证额外评论调用为 0。隔离报告位于 `builds/sorftime-reviews-live/20260929T115704Z-86ac57ef/`。
- 临时试用页地址仍是 `http://127.0.0.1:8899/#/amazon-reviews`。**9 月 30 日本次探测该端口连接被拒绝，后端没有运行**；浏览器保留的 URL 不代表页面服务在线。隔离数据目录 `builds/amazon-reviews-temp/run-76416cb5/workbench_data` 仍存在，需要重新启动对应临时实例后才能继续试用。`builds` 下测试数据被忽略，不纳入 Git。源码与进度记录不包含 API 凭据。
- 寻龙诀及其它并行工作区改动仍保留在本地，没有纳入评论模块提交。接手时重新检查 `git status -sb`，不要假定工作区干净。

## 2026-09-27 寻龙诀原版与工作台双跑结果

- 按美国站 `US`、Node ID `17242866011` 和固定策略，完成原版源码、隔离工作台各一次真实扫描，以及一次不联网的原版数据回放。详情、完整参数和差异表见 `builds/xlj-parity/US-17242866011-20260927/验收说明.md`。
- 原版与工作台实扫都为 `PARTIAL_DONE`，各发现 90 个节点，61 个分支因 Sorftime 请求失败而暂停。当前不能宣称完整扫描或“没有机会”。
- 两次实扫的节点、父子边、4,224 条月度观测、88 条判断均一致；离线回放的结构、数据和业务判断也一致。三份阶段报告通过 `xlj-mature-v3.3.1` 功能契约。
- **验收状态：未通过。** 回放多出一个趋势请求；原版和工作台的实际调用数与状态机账本不完全一致。工作台首轮批次进度缺陷导致 17 个请求在恢复时重新发出；已修复计数代码并通过回归，但这次真实运行保留了额外请求记录。Sorftime 返回 `No data available.`，客户端外层错误仍显示 TaskGroup，需在后续获准的真实验收中确认其内层原因。
- 验收目录含脱敏调用记录、独立数据库和报告、逐节点比较、请求差异、文件哈希及验证结果。未触碰 8896，隔离端口 8897；未再发请求、未递增客户版本、未推送。
- 代码验证：后端 760 passed / 2 skipped；前端 124 passed、构建通过。完整手工浏览器按钮验收未完成，因本轮浏览器控制通道不可用，实际任务通过隔离工作台 API 的相同输入契约创建。

## 1. 一分钟接手（2026-09-30）

- 当前分支：`feature/0.5.0-delivery`；本次文档保存前 HEAD 为 `cb0e93e116926b4d8404d69767b2e02cba1f346a`，领先上游 17 个、落后 0 个。Amazon 评论模块源码已在本地提交 `38f4e7458760509985219dc8fcef3886a7a2663a`；近期寻龙诀及其验收文档亦已本地提交。本次只新增进度交接提交，不推送。最新 HEAD 与 dirty 状态以实时 `git status -sb`、`git log -1` 为准。
- `0.5.0-beta.7` 是当前正式交付记录版本：`VERSION.json` 为 `released`；发布台账记录 GitHub prerelease、生产交付、技术验收 PASS、业务语义 sign-off PASS、17/17 人工值未变及可用回退备份。
- Amazon 评论模块代码、自动化结果和美国站接口实测见上节及 [`docs/AMAZON_REVIEWS.md`](AMAZON_REVIEWS.md)。`8899` 当前拒绝连接；独立测试数据目录保留，重启实例后可继续页面试用。
- 寻龙诀当前已完成本轮验收记录：Food Containers 有工作台与原版回放对照 PASS；Irons & Steamers 是工作台闭环通过；Kitchen & Dining 284507 保留为部分完成。机会结论仍受 Sorftime 对 `No data available.` 的语义未确认这一条件约束。
- Amazon Listing 图片采集页已接入工作台，并加入 FIFO 任务组调度，最多同时运行 2 组。8895 本次探测连接被拒绝；自动回归及 3 组竞争、队列补位的专项验收尚未完成。它只抓 Listing 图片，未覆盖独立爬虫项目的 A+ / Brand Story 范围。
- SellerSprite 关键词调研和寻龙诀 v4 代码都在项目中。关键词调研的真实运行状态需要按当前数据目录复核。
- 本次探测时工作区已有其他改动：`docs/WINDOWS_BUILD.md`、`frontend/src/keywordState.js`、`frontend/src/style.css`、`scripts/build-test.ps1`、`scripts/dev-launcher.ps1` 和未跟踪的 `docs/CODEX_HANDOFF.md`。这些不属于本次进度保存，应继续保留在工作区。
- 本进度文档只记录状态，不是新客户候选或发布声明。本次保存为本地提交，不推送，也不递增版本号。

## 2. 历史 Git 与工作区快照（2026-09-27）

> 以下快照和当时的提交范围记录于 2026-09-27，现已由上方 2026-09-29 Amazon 评论模块和 2026-09-30 寻龙诀验收快照补充；它们不代表当前 Git 状态。

### 快照基线

文档更新前的现场核对结果：

| 项目 | 状态 |
|---|---|
| 分支 / HEAD | `feature/0.5.0-delivery` / `28412b4fec54db3c9e537ef079ea369de3a0d75a` |
| 上游 | `origin/feature/0.5.0-delivery` = `445205c` |
| ahead / behind | 2 / 0 |
| staged | 无 |
| 未提交 tracked 文件 | 14 个修改，包括 scraper 接入、开发启动/打包配置及交接文档 |
| untracked 文件 | 7 个，包括 Amazon scraper 模块、页面、文档和专项测试 |

Git 状态随新提交和后续工作改变；新对话必须重新运行 `git status -sb`、`git log -1` 和 `git diff --stat`。当前在建代码不得因本次交接提交而被覆盖、清理或误提交。

本次交接只应提交 `PROJECT_HANDOFF.md` 与本文件；不推送远端。文档提交不代表 Amazon scraper 或寻龙诀的验收完成，也不递增 `VERSION.json`。

### 近期提交

- `28412b4`：加入寻龙诀 v4 模块和工作台入口等，含集成及来源测试文件；代码已提交本地，仍未推送。
- `dd7fceb`：`接着这个会话，我们接力开发`。
- `445205c`：当前上游 `origin/feature/0.5.0-delivery`。

## 3. 产品与版本交付状态

### 已交付基线：0.5.0-beta.7

`VERSION.json`、`RELEASE_LEDGER.json`、`docs/DELIVERY_STATUS.md` 和 `docs/ACCEPTANCE-0.5.0-beta.7.md` 一致记录：

- 版本：`0.5.0-beta.7`，schema 8，`release_status = released`；GitHub prerelease `v0.5.0-beta.7`。
- 交付构建：`0.5.0-beta.7+82379d2cd6a9`，源码提交 `82379d2cd6a97229a1dbe48d94d581cee5ddd565`。
- 台账记录的生产地址：8765；数据目录：`K:\Bobbytagging\Data`；技术验收和业务语义签字均 PASS；17 个已接入人工值逐项未变；回退备份已保留且回退验证通过。
- 这些是该版本的历史交付证据，不代表当前本机 8765 正在运行，也不代表本地 HEAD `28412b4` 的后续开发增量已经验收或发布。

## 4. 当前工作流与模块

### A. Amazon Listing 图片采集（进行中，待验收）

- 入口：工作台主导航“Amazon 图片采集”，当前测试页 `http://127.0.0.1:8895/#/amazon-scraper`。
- 可粘贴 ASIN 或 Amazon 链接；按站点识别并去重；成功后生成含 ASIN、标题和内嵌 Listing 图片的 Excel，导入时新建工作表。任务数据落在配置数据目录的 `amazon-scraper/<job-id>/`，支持进度、取消、失败重试和重启后继续。
- **队列规则：**全局最多 2 组采集任务同时执行，其他任务按 FIFO 等待；释放名额后自动补位；队列显示位置，等待任务可取消。组内页面并发另由 `stable` / `balanced` / `fast` 控制，分别为 1 / 2 / 最多 4 个页面。所以“最多两组”不是“最多两个网页请求”。
- 有 CAPTCHA、429 或 503 时降至单并发并冷却；连续 3 次受限则暂停并保留检查点。不会自动解 CAPTCHA，也不持久化浏览器 Cookie/Profile。浏览器需使用系统 Edge/Chrome 或由用户手动运行 Playwright Chromium 准备脚本；代理配置在数据目录 `amazon-scraper/proxies.txt` 或环境变量。
- 本轮读取 `/api/amazon-scraper/jobs` 得到空列表；当前 K 测试数据中没有留存采集任务。8895 `/api/health` 当前报告 dirty dev build 和隔离数据目录。
- **已知验收缺口：**`tests/test_amazon_scraper.py` 已存在，但本轮没有运行自动化测试；还没有专项证明 3 组任务同时提交时前两组运行、第三组排队、槽位释放后按序启动。真实 Amazon 网络、重启恢复、取消和导入 Excel 也需按验收清单留证。
- 之前对话曾报告美国站 5 个 ASIN 冒烟结果 5/5；当前数据目录没有可复查的运行记录，故只作历史会话记录，不算本地留存的验收证据。
- 与参考的独立 Amazon 爬虫相比，当前集成范围明确限于 Listing 图；尚无 A+ / Brand Story 抓取。接下来如决定扩展，先对照 `docs/AMAZON_SCRAPER.md` 和独立项目能力清单，不默认宣称功能等价。
- 文件入口：`backend/amazon_scraper.py`、`backend/app.py`、`frontend/src/amazonScraper.jsx`、`frontend/src/amazonScraper.css`、`docs/AMAZON_SCRAPER.md`、`tests/test_amazon_scraper.py`。

### B. SellerSprite 关键词调研（代码已接入，真实结果需复核）

- 作为工作台内与商品打标并列的独立工具页，代码位于 `backend/keyword_research/`，前端主要入口为 `frontend/src/keywordResearch.jsx` 与 `frontend/src/keywordState.js`；运行记录写入当前数据目录 `keyword-research/<run_id>/`。
- `docs/关键词调研接入-交接.md` 是 9 月 23 日起的历史交接，记录了美国站 5 ASIN “矩阵关”成功生成 11 Sheet 报告；“矩阵开”当时没有成功真实产物。该记录还记有后续标注健壮性和连接重试修复、聚焦离线测试 10 passed。它的分支、提交数和运行结果没有在本次逐项重放，不可直接当作当前现场。
- 新对话要先检查当前分支代码、`docs/关键词调研接入-交接.md`、最近运行目录及报告文件，再说明目前真实取数和矩阵状态；不要把离线测试等同于 SellerSprite / 模型真实端到端验收。密钥由用户在工作台设置，不要从其他目录复制或写入交接文档。

### C. 寻龙诀 v4（当前源码实测通过；机会语义附条件）

- 历史 `US-17242866011-20260927` 运行仍为部分完成，不能用来代表本次结果。新证据见上方 2026-09-30 章节及 [`LOOP_STATUS.md`](../LOOP_STATUS.md)：`21551230011` 原版/工作台回放对照通过，`510240` 工作台扫描闭环通过，`284507` 部分完成且 `completion_ok=false`。
- 本次运行过程中 `No data available.` 被适配层仅在已验证的非根节点、已完成量价趋势及有效树任务上下文下映射为空直接子类目；错误上下文保持失败，不会成为机会。Amazon 当前类目页面支持末端位置判断，但 Sorftime 官方尚未确认此纯文本响应与空子类目等价。
- 运行和验收都使用隔离存储，不递增客户版本号、不推送。`510240` 是工作台闭环验证，没有原版对照；`284507` 不作为成功验收。后续如需把条件性机会语义升级为官方确定，需要 Sorftime 明确书面说明响应约定；不要重复扫描同一节点来代替语义确认。

### D. 核心商品打标工作台

- beta.7 已交付能力包括 Agent 运行时、任务执行与恢复、标签标准反推、人工值保护、文本/视觉批处理、Excel 图片导入导出及 Windows 打包；详见 beta.7 验收记录和 `CHANGELOG.md`。
- beta.7 验收文件记录当时后端 596 项通过及 Phase A/B 真实模型和安装/升级/回退验证。那是绑定旧提交的历史证据，不是当前脏工作区的完整回归结果。
- `docs/CODEX_HANDOFF.md`、较早的 `PROJECT_HANDOFF.md` 正文、关键词交接文档中含旧 HEAD、分支 ahead 数、测试数或 staged 状态。它们保留作历史背景；若与本文件或实时 Git/health 冲突，以根目录 `AGENTS.md` 的核验流程和本次最新实时证据为准。

## 5. 本机运行状态（2026-09-30 本次探测）

| 地址 | 本次结果 | 可得结论 |
|---|---|---|
| `http://127.0.0.1:8899/api/health` | TCP 连接被拒绝 | Amazon 评论临时页后端当前未运行；`builds/amazon-reviews-temp/run-76416cb5/workbench_data` 数据目录仍存在。 |
| `http://127.0.0.1:8901/api/health` | `status=ok`；build `0.5.0-beta.7+38f4e7458760`；commit `38f4e745…`；`dirty=true`；`storage=C:\Users\Administrator\AppData\Local\Temp\GeWu-XLJ-US-21551230011-20260929` | 寻龙诀隔离验收实例仍在响应，绑定旧的 21551230011 数据目录；这不代表新候选 510240 共用该目录。 |
| `http://127.0.0.1:8895/api/health` | TCP 连接被拒绝 | Listing 图片采集开发实例当前未运行；历史任务清单与 9/26 探测结果不代表本次运行状态。 |
| `http://127.0.0.1:8765/api/health` | TCP 连接被拒绝 | 本次没有取得生产健康信息；不能据此推断正式安装或生产数据状态。发布台账中的 beta.7 交付记录仍是历史确认状态。 |

`K:\Bobbytagging\Data` 是正式数据目录，继续只读保护。任何后续测试、升级或取数须使用 `K:\GeWuTest\Data` 或另一个明确隔离副本，不要把生产目录作为测试数据源。

## 6. 下一步建议

1. 若接着当前 Amazon scraper：先做队列调度专项验收（3 组竞争、FIFO、完成/取消后补位、等待项取消），再运行相关测试和真实美国站小批量回归；保存运行记录及 Excel 哈希到隔离数据目录，明确人工验收状态。无需重新讨论反爬节奏设计，按项目内 scraper 参考与 `docs/AMAZON_SCRAPER.md` 执行。
2. 复核 SellerSprite：从当前代码和当前数据目录确认运行记录，检查矩阵开/关真实产物与 API 调用账，避免沿用旧分支计数。
3. 寻龙诀本轮工程验收已通过。若需消除机会语义条件，向 Sorftime 核实 `category_tree` 对无数据文本的正式含义，并保留书面证据；本轮授权只覆盖已完成的隔离测试，不代表生产数据操作。
4. 之后若要冻结客户候选，再按 `docs/RELEASE_GOVERNANCE.md` 选择从未用过的新 beta 号，并更新版本、台账、变更记录和派生元数据；本次交接不做版本递增或推送。

## 7. 新对话启动指引

新对话从仓库目录开始，按 `AGENTS.md` 顺序核对 Git、交接文件、版本/台账、变更记录和本次任务材料。先读本文件的“当前 Git 与工作区”和对应模块状态；涉及运行程序时重读 health，不引用对话记忆替代核验。要继续项目而非重做背景梳理时，可直接把“按 `docs/PROJECT_PROGRESS.md` 接手当前进度”作为新任务说明。
