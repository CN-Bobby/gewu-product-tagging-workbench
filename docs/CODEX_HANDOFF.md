# 交接文档：格物工作台 · 三层运行模式（开发测试层已落地）

- 交接时间：2026-09-17
- 交接人：Qoder（施工方）
- 接手人：Codex（技术负责人 / reviewer）
- 施工方：Qoder CN CLI（执行工程师，见 §7）
- 本文件性质：**当前事实 + 已核证据 + 未决项 + 硬约束**。不是方案讨论稿，不需要重新论证方向。

---

## 1. 你要做什么

你是本仓库的技术负责人：定任务规格、审代码与证据、判定结果。Qoder CLI 是执行工程师，按你给的任务规格施工，**不做架构决策**。

- 你负责：任务拆解、契约/范围/安全判断、验收判定、result 分类（`COMPLETE` / `NEED_REWORK` / `BLOCKED`）。
- Qoder 负责：按规格改代码、跑测试、给证据。
- 你**不要**接手批量编辑、机械式代码检索、lint 清理、普通测试失败修复——那些属于执行方。只有架构、范围、不变量、契约、安全风险才需要你重新进入实现决策。

下一轮该做什么，见 §6「未决项」。是否推送当前未推送的提交，见 §5「待你裁决」。

---

## 2. 仓库与基线状态（2026-09-17 实测）

| 项 | 值 |
| --- | --- |
| 仓库根 | `D:\数据打标项目` |
| 分支 | `feature/0.5.0-delivery` |
| HEAD | `0fbbc25` |
| 工作区 | 干净（写入本文档后变为有未跟踪文件，见 §8「注意」） |
| 与 origin 差距 | `0 5`（**5 个提交已提交未推送**） |
| VERSION.json | `0.5.0-beta.7` / channel `beta` / schema_version `8` / reasonix_baseline `1.25.1` / release_status `released` |

**未推送的 5 个提交（自旧到新）**

| commit | 内容 | 变更规模 |
| --- | --- | --- |
| `8fb7fda` | `feat(agent)`: 一个回合的工具事件折叠成一张活动卡片 | 6 文件 +565/−35 |
| `0d653fc` | `feat(agent)`: 工作台提案与任务投影成视图模型 | 2 文件 +331 |
| `5c189ce` | `feat(dev)`: 让不安装的开发构建与安装器并存 | 9 文件 +280/−15 |
| `3cc9df2` | `docs(build)`: 记录不安装的开发测试流程 | 1 文件 +12 |
| `0fbbc25` | `fix(dev)`: 构建目录名由整个工作区状态派生 | 3 文件 +69/−7 |

前两个（`8fb7fda`、`0d653fc`）属「标签变更提案卡片交互改造」工作流，后三个属本次「三层运行模式」。它们同在一批未推送提交里。

**测试基线（在 `0fbbc25` 上重新实测）**

- 后端：`.venv/Scripts/python.exe -m pytest -q` → **598 passed**（205.91s）
- 前端：`cd frontend && npm test` → **67 passed / 0 failed**
- `scripts/repository_audit.py` → `{'tracked_files': 268, 'forbidden_paths': [], 'secret_hits': [], 'sample_hits': []}`，exit 0

**已交付的正式版本（不要动）**：tag `v0.5.0-beta.7` → commit `82379d2`，GitHub prerelease 已发布。生产实例跑 `0.5.0-beta.7+82379d2cd6a9`。

---

## 3. 三层运行模式：现在长什么样

### 第 1 层 · 开发测试（已落地、已实测）

不安装，构建产物直接是可跑目录。

| 入口 | 实现 | 作用 |
| --- | --- | --- |
| `构建测试版本.cmd` | `scripts/build-test.ps1` | 一键：读版本号 + short SHA → 构建 → 写 `builds/current.json` 指针 → 启动 |
| `开发测试工作台.cmd` | `scripts/dev-launcher.ps1` | 固定入口，只读 `current.json` 起指定版本；换版本只改指针，不重装 |

- 产物：`builds/<version>+<sha12>[-dirty.<worktree_sha256 前 8 位>]/app`（约 152 MB / 2626 文件）
- 命名：`worktree_digest()`（`scripts/build_windows.py`）对 `git diff HEAD --binary` + 全部未跟踪新文件内容取 SHA256，**只作目录命名输入**，不写进 `source_sha256` / health / job spec
- **既有目录一律拒绝覆盖**（exit 1，文案可诊断）。要重建同一个工作区状态，只能先手工删该目录
- dev 构建在 NSIS / 源码 ZIP / SHA256SUMS 之前提前退出，**不产出任何可被误认为发布物的文件**
- 历史版本全部保留在 `builds/`（当前 3 个：`+3cc9df22cc47`、`+0d653fc847e2-dirty.4784291b`、`+0fbbc255e561`），`current.json` 指向 `0.5.0-beta.7+0fbbc255e561`
- 数据：默认 `K:\Bobbytagging\Data-Test`（cache 默认 `<storage 上级>\Cache-Test`）
- 端口：由数据目录路径 SHA256 推导 `8766 + hash[0] % 40`；**8765 保留给安装版**

### 第 2 层 · Real Acceptance（入口已具备，尚未跑过正式验收）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev-launcher.ps1 -Storage K:\Bobbytagging\Data -Acceptance
```

必须**显式**同时给出 `-Storage` 与 `-Acceptance` 才可能指向生产数据目录，且会打印醒目告警块。未跑过。

### 第 3 层 · 正式发布（本轮完全未动）

`releases/` + NSIS 链路原样保留。仅用于 Release Candidate / 最终 Real Acceptance / 正式交付 / 安装升级机制本身的专项测试。

### 安全闸门（已实测生效）

`dev-launcher.ps1` 只以**只读**方式读 `%LOCALAPPDATA%\GeWuWorkbench\desktop.json` 里的 `data` 作为 **deny-list**：目标等于安装版数据目录且未带 `-Acceptance` → 明确报错拒绝启动。

该闸门是 **fail-closed**：`desktop.json` 存在但读不出、或没有 `data` 字段 → 直接拒绝启动并给出可诊断消息，**不允许用 catch 降级放行**。（早期版本用 `try/catch` 静默吞掉，导致保护静默失效，已修。任何守卫都按此规则。）

### 身份显示

- `build-manifest.json` 记录：version、build_id、commit、branch、build_mode（dev/candidate/release）、storage_mode、dirty、built_at，脏构建另记 `worktree_sha256`
- 顶栏显示实际运行的 `build_id`（如 `0.5.0-beta.7+0fbbc255e561`）+ 脏工作区徽标
- 「模型设置 → 版本技术信息」列出版本、build_id、release_status、commit、branch、build_mode、storage_mode、dirty、built_at

---

## 4. 已验证事实（附证据与验证手法）

以下都是**本机本会话实测**，不是推断，也不构成平台级通用保证。

1. **程序目录运行时零写入**：无 `__pycache__` / `.pyc` / 日志落入 `app/`（launcher 设 `PYTHONDONTWRITEBYTECODE=1`，路径全外置）。
2. **无残留文件锁**：`Workbench.exe --stop` 后端口、程序目录、数据目录（含 `runtime.lock`）全部可释放；`runtime.lock` 是 `msvcrt` 区域锁，进程死亡即释放。
   *陷阱*：在 Git Bash 里测「目录是否仍被锁」会因**自己 shell 的 CWD 停在该目录**而假阳性，必须在 CWD 不在该目录的 PowerShell 里复测。
3. **一次完整 dev 构建实测 10 秒**（新目录、2626 文件、152 MB、`--allow-dirty`）。所以「高频改前端要秒级起实例」不需要额外快车道。
4. **provider 天然按数据目录隔离**：`DesktopLauncher.cs` 把 `TAG_PROVIDER_HOME` 派生为 `<--data>/providers`；dev 实例上 `GET /api/settings/models` 实测 `credential_path = K:\Bobbytagging\Data-Test\providers\.env`。`%APPDATA%\reasonix` 只是**源码直跑**时的全局回落。
   → provider 配置存在**数据目录**而非 build 目录：在 Data-Test 配一次，后续所有 dev build 复用。
   **前提**：必须经 `Workbench.exe --data` 启动；直接跑 `runtime\pythonw.exe scripts\desktop_bootstrap.py` 会落到全局 `%APPDATA%\reasonix`。
5. **生产数据目录本会话零改动**：`K:\Bobbytagging\Data` = 537 文件。当天唯一一批写入是本地 05:44–07:19（UTC 2026-09-16T21:44–23:19），含 `backups/upgrade-20260916T231915515425Z/workbench.db` + `installed-build.json` + `desktop-endpoint.json`，即**安装版在那时被启动/升级过一次**，比本会话开始早约 15 小时。最新写入 = `workbench.db-wal` @ 2026-09-16T23:19:51Z。本会话与任何 dev 构建都未触碰它。
6. **字节级重建不可复现的范围比 `docs/WINDOWS_BUILD.md` 写的更广**：两份 `source_sha256` 相同的构建，差异在 `Workbench.exe`（csc PE 时间戳 / MVID，文档已提）**以及 pip 生成的 `runtime/Lib/site-packages/bin/*.exe` 与其 `dist-info/RECORD`**（文档未提）。→ 任何「整包逐字节一致」的声称都不成立；身份用源码摘要而非整包哈希是对的。
7. **目录命名修复的变异证据（4/4 CAUGHT）**：对新增守卫 `test_dev_build_directory_name_covers_every_worktree_change` 注入 4 个变异——M1 改回用 `source_sha256` 命名、M2 删未跟踪文件循环、M3 删 `git diff HEAD --binary`、M4 删 `-dirty.` 判别符——全部如实变红，基线 GREEN。
8. **修复前后探针**：探针 A（原本撞名死胡同的场景）现在得到 `-dirty.14a65eab`；探针 B（原本 `exit 1` 的场景）得到 `-dirty.e307da1e` 且 exit 0。探针产物已删。
9. **清工作区回归**：`build-test.ps1 -NoLaunch` 构建出 `0.5.0-beta.7+0fbbc255e561`、`dirty=False`、manifest **无** `worktree_sha256`（dev-only 字段不污染清构建）。
10. **收尾状态**：端口 8765 / 8771 均无监听，无残留 `Workbench` / `pythonw` / `reasonix` 进程。

---

## 5. 硬约束（必须原样传给执行方）

以下是不变量。任何会破坏它们的任务，执行方应停下报 `BLOCKED`，不得自行扩权。

**数据安全**
1. `K:\Bobbytagging\Data` 是生产数据目录，**只读**。其中的 310 行生产数据与 17 条人工标签**不得修改**。
2. 一切验证必须使用隔离 / 临时 storage，且在工作区之外。
3. 业务数据、数据库、凭据**不得**写进 build 目录或程序目录。
4. 凭据**不得**跨数据目录复制（包括「顺手拷一份配好的 providers/.env」）。provider 首次配置需用户自己的密钥，agent 不得代填。
5. 删除任意一个 `builds/` 子目录**不得**影响用户数据。

**构建与发布**
6. dev 构建**不得**覆盖既有 `builds/` 目录。
7. dev 构建**不得**写入 `releases/`，不得调用 `makensis`，不得产出可误认为发布物的文件。
8. 开发版**不得**默认连接生产数据目录；未显式 `-Acceptance` 时必须拒绝。
9. 针对正式发布产物的规则：**已验收的 artifact 不因状态提升而重建**；tag 指向实际构建/安装的 commit；上传后要下载回算哈希。
10. `releases/` 与 NSIS 模板在非发布轮次里不得改动。

**提交纪律**
11. **一轮一提交**，提交消息用用户原文；默认**只提交不推送**；推送 / 打 tag 需**逐次授权**。
12. 提交前统一 stage 并核对文件集合；报告须给 `git status` / commit 号 / ahead-behind。
13. 改契约或事件名的轮次**必须**做 mutation（非空转）检查：注入错误实现，验证新断言真会变红；变异要忠实，逐条给 FAILED 证据。
14. 测试/审计计数必须可复现，并注明口径。
15. 起进程/线程的测试要把清理登记进夹具，失败路径也不许泄漏。

**代码边界**
16. 能在高层（Adapter / launcher）表达的就别改低层（Runtime / 共享契约）。要改低层，必须同时具备：确定性 red test + 低层无法满足 + 最小高层修复不成立；否则停下报 blocker。
17. 生产代码只表达直接观察到的事实，禁止推断性状态字段。
18. 严格校验放在自己模块边界，不改公共组件通用语义；不得静默吞掉错误（要么类型化报错，要么记诊断）。
19. 同一事实只许一个真相源。禁词 grep 指「不得依赖」而非「不得出现字面量」，不要为过 grep 做规避。
20. `scripts/*.ps1`、`*.cmd` **必须纯 ASCII**：Windows PowerShell 5.1 按 ANSI(GBK) 读无 BOM 的 `.ps1`，中文字符串会破坏解析。中文只允许出现在文件名里。
21. UI 改动必须用真实浏览器在隔离实例上走通（强制取新文档避免测旧 bundle，DOM 程序化读取）；付费真实回合先授权。

---

## 6. 未决项（供你决定下一轮做什么）

### 6.1 安装 / 升级机制专项（**用户已明确延后到独立轮次**）

已定位到行的 6 条根因，按证据强度排序：

1. 运行中的 exe 锁定程序目录 + NSIS `SetOverwrite try`（`scripts/build_windows.py`）**静默跳过**写入失败 → 同时解释「覆盖失败 / 旧文件残留 / 安装看起来成功」。
2. `--stop` 门可能**假成功**：只对 `Same(health, data)` 的实例生效，且 `data` 来自全局 `desktop.json`；换端口/换 data 起的同目录实例会被 `return 0` 假装停掉。
3. **升级从不删旧文件**：卸载 Delete 清单 = 构建时该版本的文件集，上一版有、这版没有的文件结构性残留。
4. `desktop.json` 全局共享 → 安装器停机动作可能误停生产实例。
5. InstallMode 判定只靠 2 个文件存在性 + `.gewu-installing` 标记，半完成安装会让下次走不同分支。
6. 单目录单版本 by design → 高频迭代下装安装包必然痛苦。**这不是 bug**。

⚠️ **红test预警**：改 `SetOverwrite try` 会让 `tests/test_versioning.py::test_release_ledger_and_single_install_identity` 的 **8 处 NSIS 片段断言变红**。契约断言必须同步更新，且该轮需单独一审。

### 6.2 `K:\Bobbytagging\Data-Test` 尚未配置 provider

后果：Agent 会话（标签提案卡片交互的前提）在该数据目录下跑不起来。

**这不是代码缺陷**——provider 存在数据目录里（见 §4.4）。用户在 Data-Test 界面里配一次即可，之后所有 dev build 复用。launcher 已给告警，不会、也不得复制生产凭据。首次配置需要用户本人的密钥。

### 6.3 `source_sha256` 覆盖范围窄于产物决定集（低层契约，**未立项**）

`backend/build_info.py` 的摘要只覆盖 `VERSION.json`、`requirements.txt`、`backend/`、`frontend/src/`、`skills/product-tagging/`；**不含** `scripts/`（含会进产物的 `app/scripts/desktop_bootstrap.py`，以及编译成 `Workbench.exe` 的 `DesktopLauncher.cs`）与 `frontend/package.json` + lockfile。

后果：两个实质不同的构建可携带同一份「源码身份」（实测产物内文件哈希变了、`source_sha256` 一字未变）。dev 层的**撞名死胡同**已修（改用 `worktree_sha256` 命名），但 `source_sha256` 本身是否该扩范围**未动**——它被 `/api/health`、`BatchJob.spec['application_build']`、发布 manifest 共用，改语义属低层契约变更，按约束 16 需独立一轮 + red test。

### 6.4 源码直跑路径仍在（并占用 8765）

`启动工作台.cmd` / `停止工作台.cmd` → `scripts/start.ps1` / `stop.ps1`：用 `.venv` 起 `uvicorn backend.app:app`，默认 storage = `<repo>/storage`，**默认端口 8765（与安装版冲突）**，且**不设** `TAG_PROVIDER_HOME` → provider 落到全局 `%APPDATA%\reasonix`。

这正是三层模式想取代的路径，目前仍在。是否退役 / 改指向 dev 层，未决。

### 6.5 观察到但未处理

`build-test.ps1` 输出里有既有 pip 解析告警：`comfy-mcp 0.10.0 requires mcp<3,>=2, but you have mcp 1.29.1`。与本次改动无关。

---

## 7. 你怎么指挥 Qoder（本地通路已就绪）

按 `~/.agents/skills/agent-team/SKILL.md` 执行。要点：

**执行器**
- Qoder CN CLI：`C:\Users\Administrator\.qoder-cn\bin\qoderclicn\qoderclicn.exe`
- 版本 `1.1.61`，登录态正常（`qoderclicn status` → Username `aliyun7882947603`）。**登录只能交互式**，需要重登请让用户自己敲 `qoderclicn login`。
- 委派助手：`C:\Users\Administrator\.agents\skills\agent-team\scripts\delegate.py`

**模型必须现查，不得硬编码**

```bash
"C:\Users\Administrator\.qoder-cn\bin\qoderclicn\qoderclicn.exe" --list-models
```

2026-09-17 实测该命令可用的 DeepSeek 条目有多个（含 `DeepSeek-V4-Pro`、`DeepSeek-Flash` 等）。因为多于一个，助手不会自动选，会报 `DEEPSEEK_MODEL_REQUIRED`，**你必须在 `--model` 里给出精确 ID**。

⚠️ **坑**：在 Qoder 自己的 SDK 宿主 shell 里跑 `--list-models` 会失败并报 `sdk_invalid_args: Agent SDK entrypoint env is set but required flags are missing`，因为该环境设了 `QODER_AGENT_SDK_ENTRYPOINT`。在干净 shell 里跑，或先 `unset QODER_AGENT_SDK_ENTRYPOINT`（以及 `QODER_SDK_AUTH_PAYLOAD_FILE`、`QODER_AGENT_SDK_VERSION`）。

**委派命令（两步，中间有一道用户审批门）**

助手**没有**"一条命令直接派发"这种用法。新派发必须走"冻结计划 → 用户批准 → 再派发"。

第一步 · 冻结计划（此阶段不动仓库，只写外部 state root）：

```bash
python "C:\Users\Administrator\.agents\skills\agent-team\scripts\delegate.py" \
  --prepare-plan \
  --repo "D:\数据打标项目" \
  --task-file <你的任务规格.md> \
  --model <现查到的精确 DeepSeek ID> \
  --reasoning-effort <冻结的推理档位> \
  --allow-path <仓库相对路径或 glob，可重复>
```

它打印 `plan.json` 的 SHA-256。**把计划、哈希、允许路径、测试、风险、base SHA 展示给用户，停在 `AWAITING_PLAN_APPROVAL` 等用户明确批准**——不要拿"对项目的笼统认可"当批准。

第二步 · 派发（只有用户批准后才执行）：

```bash
python "C:\Users\Administrator\.agents\skills\agent-team\scripts\delegate.py" \
  --approved-plan <plan.json> \
  --approved-plan-sha256 <用户批准的那个哈希> \
  --plan-approval <批准引用，如批准那一轮的时间戳或任务专属标签>
```

后续环节还有：review 回传（`--mark-review --review-decision NEED_REWORK|AWAITING_MERGE_APPROVAL|BLOCKED --review-reference ...`）、有界返工（`--continue-run`）、整合（`--integrate-run`，需**单独的**合并授权）、中断恢复（`--recover-run`）。**计划批准与合并批准是两道独立的门，不能互相推断。** 完整开关集见 `delegate.py --help`。

**任务规格骨架**（照 skill 的段落结构写，只放与任务相关的细节）

```
# Agent Team Task
## Objective
## Repository
## Base State        ← branch / HEAD / working tree，必须照抄 §2 的实际值
## Scope
### Allowed
### Avoid Unless Necessary
## Requirements
## Invariants        ← 抄 §5 里与该任务相关的不变量，不要省略
## Acceptance Criteria
## Verification
## Escalation Conditions
```

升级条件（命中就让执行方停下报 `BLOCKED`，不要靠扩大架构绕过去）：需要改核心公共契约；范围必须实质扩大；一个计划关键假设为假；必须破坏某条不变量；发现高危安全问题。

**留痕（产物在仓库之外）**：默认 state root 是 `~/.codex/agent-team/`，即 `C:\Users\Administrator\.codex\agent-team\`。`validate_external_state_root()`（`delegate.py:118`）**明确拒绝任何位于仓库或 Git 公共目录之内、或把仓库包含在内的 state root**（`raise ValueError("state root must be outside the repository and Git common directory")`），所以**产物不会落进仓库**。

- `plans/<UTC时间戳>-<8位hex>/`：`task.md`、`plan.json`、`plan.sha256`
- `runs/<run_id>/`：`manifest.json`、`events.jsonl`、`attempts/NNN/`（`task.md`、`qoder-output.txt`、`changes.patch`、`artifacts.json`、`result.json`、`evidence.sha256`）、`reviews/NNN/`、`integration/`
- `worktrees/<run_id>/`：git worktree

可用 `--state-root` 改到别处，但同样必须在仓库之外。助手会记录改动前后的 branch / HEAD / git status。

⚠️ **不需要给 `.agent-team/` 加 `.gitignore`**（2026-09-23 修正）：产物全部落在 `~/.codex/agent-team/`，**在仓库之外**，委派本身不会让工作区变脏。本节早先版本基于"产物落在 `<repo>/.agent-team/`"的错误前提建议加 gitignore，该建议已作废。

**review 后分类**
- `COMPLETE`：实现与验收证据满足规格。
- `NEED_REWORK`：方向有效，只剩有界的实现/测试缺陷 → 给一个聚焦的纠偏任务，不要重新设计系统。
- `BLOCKED`：命中升级条件、执行器不可用、缺授权、或存在须决策的重大架构问题。

**你 review 时要做的**：读回传的执行报告、executor 退出码、最终 `git status`、diff stat、run 目录；然后**只重跑**建立信心所需的关键验收测试，不要重跑全套。

---

## 8. 用户可以亲手验证的步骤（不需要重装任何东西）

1. 双击 `构建测试版本.cmd` → 应输出 `builds/0.5.0-beta.7+<sha12>`（工作区干净时无 `-dirty` 后缀），并自动打开工作台。
2. 看顶栏徽标与「模型设置 → 版本技术信息」：应显示同一 `build_id`；`storage` 显示 `K:\Bobbytagging\Data-Test`；`build_mode` = dev。
3. **再跑一次同样的命令** → 应被拒绝，提示该目录已存在、删掉才能重建。这是预期的防覆盖行为，不是故障。
4. 随便改一行 `backend/` 或 `frontend/src/` 再跑 → 应生成一个**新目录**，旧目录原样保留；`开发测试工作台.cmd` 只改 `current.json` 指针即可切回旧版本。
5. 调回上一版本：手工把 `builds/current.json` 的 `current` 改成旧目录名，再跑 `开发测试工作台.cmd`。
6. **失败判定**：若 `current.json` 指向的目录里 `app\Workbench.exe` 不存在，或 health 的 `storage` 不是 `Data-Test`，即为异常——把 `K:\Bobbytagging\Data-Test\logs\desktop.log` 取出来。

**注意**：本文档写入后工作区由「干净」变为「有未跟踪文件」。若要恢复干净状态并让 dev 构建不带 `-dirty.` 后缀，把本文档单独提交一次即可（按约束 11，默认只提交不推送）。

---

## 9. 用户新报的两个缺陷与一个合并要求（只读核查，本轮未改任何代码）

用户在最新版本上实测报出三项。以下是我在 `0fbbc25` 工作区上做的**只读**定位：读了全部相关源码，另用 SQLite `mode=ro` 读了两套数据目录的库结构（见 §9.6 的副作用披露）。**没有修改任何文件**（唯一新增文件是本文档本身）。

### 9.1 用户原话（三项现象）

1. "在对话框聊完标签定义，要么执行卡片会弹到对话框的切换页，要么就不弹出来。而且手动在网页上输入定义之后，也不会在对话框中弹出执行按钮。"
2. "切换表格之后，默认留在旧对话，继续对话不会自动读取当前的新表格数据，需要 agent 提示之后手动切换，或者新建对话。"
3. "网页左边的 agent 对话，新旧两种切换，应该不需要，可以合并掉。"

### 9.2 缺陷一：提案卡片在 Agent 面板上根本不存在，且在旧面板上只按对话过滤

**这是两个互相叠加的缺陷，任一个都足以解释现象。**

**(a) 唯一能确认提案的 UI 整块长在 LEGACY 分支里，而默认面板是 AGENT。**

- `frontend/src/agentState.js:19-20`：`PANEL={AGENT:'agent',LEGACY:'legacy'}`、`DEFAULT_PANEL=PANEL.AGENT`。
- `frontend/src/main.jsx:55`：`useState(DEFAULT_PANEL===PANEL.AGENT)` → 打开就是 Agent 面板。
- `frontend/src/main.jsx:112`：`{agentMode?<AgentPanel did={did}/>:<>…` —— **确认/执行/抽样执行/撤回四个按钮和「确认商品范围」下拉全部在 `<>` 那个 else 分支里**（`main.jsx:114` 的 `{currentProposal&&proposalCard(currentProposal)}`）。
- `frontend/src/agentPanel.jsx`（236 行）渲染的只有：状态条、错误条、`TurnActivityCard`/消息列表、`InteractionCard`、输入框。**全文没有任何 proposal 相关代码**——它甚至不读 `data`，只接一个 `did`。

所以用户在 Agent 面板里聊完标签定义，**界面上没有任何地方能出现执行按钮**，符合"就不弹出来"。

手工新增标签那条路径更明确地暴露了这个错位：`main.jsx:142` 的「新增标签」modal 走 `propose([{kind:'add',…},{kind:'execute',…}])`，成功后的提示是 `setNotice('提案已准备，请在对话中一次确认。')`（`main.jsx:74` 的 `propose`）——**提示让用户去"对话中确认"，而那个确认按钮在另一个 tab 上**。这就是"手动在网页上输入定义之后，也不会在对话框中弹出执行按钮"。

**(b) 提案事件本身不带 `conversation_id`，所以旧面板的过滤在事件到达那一刻必然为假。**

- `backend/db.py:199-211 class Proposal`：有 `origin_turn_id`，**没有 `conversation_id` 列**。
- 事件 payload 由**表列序列化**产生：`backend/db.py:428 def obj(record): return {c.name: getattr(record, c.name) for c in record.__table__.columns}`。没有这一列，`obj()` 就永远产不出这个键。
- 四个发出 `proposal` 事件的位置全部走 `obj()`：`backend/draft.py:313`（旧提案置 superseded）、`:314`（新建提案）、`:461`（另一条新建路径）、`:632`（确认后回写）→ **流上的 proposal payload 结构性缺 `conversation_id`，不是偶发丢字段。**
- `conversation_id` 只在 REST 路径上被合出来：`backend/chat_threads.py:139-143 decorate()` 给每条 message/proposal 打上 `conversation_id`（proposal 用 `turns.get(origin_turn_id, default)`）。
- 而工作台读的是**事件流**：`frontend/src/workbenchEvents.js` 的 `applyDraftEvent` 对 `proposal` 事件走 `COLLECTION_BY_KIND`→`mergeById(data.proposals, payload)`，即**把流里的原始 payload 直接并进 `data.proposals`**，payload 里没有 `conversation_id`。
- 于是 `frontend/src/main.jsx:106` 的
  `p.status==='pending'&&p.conversation_id===currentConversation`
  在提案刚产生的那一刻**恒为 false**（`undefined===当前对话id`）→ 卡片不弹。

刷新一次之后才可能看见，因为重新 `GET /api/drafts/{id}` 走了 `decorate()`，所有没有 `origin_turn_id` 的提案被统一盖上该数据集的**迁移默认线程**（`chat_threads.py:10-19 ensure()` 造的那个 `'历史对话'` 线程，`legacy:True`）。**用户当前选的对话只要不是那一条，卡片就不见**；恰好切到那一条才"弹出来"——这正是"要么弹到对话框的切换页，要么就不弹出来"。

**(c) 让 (b) 变成"总是错位"而不是"偶尔错位"的根因：`origin_turn_id` 从来没被写过。**

- 写入端读的标记：`backend/draft.py:286-288 _active_turn()` 读 `WorkbenchMeta('active-chat:'+did)`，且要求 `item.kind=='active_chat_turn'`。
- 全仓库对这两个字符串的引用只有：读 `chat_threads.py:72,89`、`draft.py:287`；删 `db.py:408`、`jobs.py:78`（`__pycache__` 之外的源码里）。**没有任何地方写它。**
- 而 Agent 侧的提案入口是带着"用当前回合"的意图来的：`backend/mcp_server.py:173 return draft.propose(DATASET,title,changes,use_active_turn=True)`；`draft.py:435` 只有 `use_active_turn and not origin_turn_id` 时才去 `_active_turn()`。读不到标记 → `origin_turn_id=None`。

**实测佐证**：`<repo>/storage/workbench.db` 里 **26 条提案的 `origin_turn_id` 全为 NULL**，全库只有 1 条 `chat_thread`。单对话时代这个错位是隐形的；一旦有第二条对话就必现。

**(d) 本该解决这件事的模块是死代码。**

`frontend/src/proposalState.js` 是 `0d653fc` 为此新写的纯投影模块，注释里点名了这个 bug（`proposalState.js:51-53`：`Proposals are scoped by dataset id, never by the legacy conversation the old card was filtered by: an agent-produced proposal belongs to no conversation, which is why that filter hid it.`），并导出了 `selectPending` / `workbenchActions` / `proposalView` / `taskView`。

**但全仓库消费它的只有它自己的测试**：`grep -rn proposalState|workbenchActions|proposalView|taskView frontend/src frontend/tests` 结果中，`frontend/src/` 下只有 `proposalState.js` 自身，其余命中全在 `frontend/tests/proposalState.test.js`。**零生产消费者**——投影层建好了，没接线。

结论：缺陷一 = 「确认 UI 在错误的 tab」+「提案事件缺 `conversation_id` 使旧过滤恒假」+「`origin_turn_id` 无写入者使错位成为默认」+「替代实现已就绪但未接线」。**修它必须同时处理切面板与接线，只改过滤条件会在 Agent 面板上仍然没有按钮。**

### 9.3 缺陷二：切换数据集后 Agent 面板复用了旧会话（会话的数据集在创建时就钉死）

- `frontend/src/main.jsx:112`：`<AgentPanel did={did}/>` **没有 `key`**。React 因此复用同一个实例、同一个 `useReducer` 状态，`did` 换了组件不重建。
- `frontend/src/agentPanel.jsx:118-124`：恢复记忆会话的 effect 依赖数组是 `[]`，**只在挂载时跑一次**；它读的是**一个全局键** `localStorage['agent:last-session']`（`agentPanel.jsx:14`），与 `did` 无关。
- 会话的数据集在创建时就绑定且之后不可变：`agentPanel.jsx:131 call('/api/agent/sessions',{dataset_id:did})` → `backend/harness.py:91` 把 `TAG_DATASET_ID=dataset_id` 写进子进程环境 → `backend/mcp_server.py:13 DATASET=os.environ['TAG_DATASET_ID']`（每进程读一次）。

于是切表格后，"留在旧对话"是**当前实现唯一可能的结果**：agent 进程里的 `DATASET` 仍是旧表，继续对话当然读不到新表数据。用户说的"需要 agent 提示之后手动切换，或者新建对话"是**唯一可行的绕过**，不是使用姿势错误。

**缺少的守卫**：客户端想做"会话与当前数据集不匹配就不要恢复"的判断，需要一个字段——`dataset_id`。而
- `backend/agent_application.py:72-74 active_sessions()` **带了** `dataset_id`（`row['dataset_id']`）；
- 但面板走的 `GET /api/agent/sessions/{id}` 最终落到 `backend/agent/bridge.py:125-129 status()`，返回只有 `{'session_id','status','remote_session_id','backend'}`——**没有 `dataset_id`**；`agent_application.py:210-221 status()` 在"非本进程创建"的分支同样只有这四个字段。

也就是说：**当前 HTTP 契约下，前端拿不到"这个会话属于哪个数据集"**。要加客户端守卫，先要扩 `bridge.status()` 与 `agent_application.status()` 的返回（属低层契约变更，按约束 16 需独立一轮 + red test）。

### 9.4 合并新旧切换：可行，但被 9.2(a) 卡住

用户要求"新旧两种切换可以合并掉"。落点已经查清：

- 开关本体：`main.jsx:55`（state）、`main.jsx:112` 的标题/切换按钮与三分支渲染。
- 默认值来源：`agentState.js:19-20`。
- **守卫测试**：`frontend/tests/panelDefault.test.js` 现在是**反向**断言——它要求默认面板是 AGENT（`DEFAULT_PANEL,PANEL.AGENT`）、要求 `main.jsx` 里存在 `{agentMode?<AgentPanel did={did}/>:<>` 这个 else 分支、并要求 "标签对话"/"Agent 会话" 两个标签都在。**合并会直接改红这个文件**，它是 `0d653fc` 一轮的产物，要连同该轮的意图一起重新裁决（改成"只有一个面板"的断言），不能悄悄删。
- **真正的阻塞点**：`legacy` 分支同时持有**唯一一份提案确认 UI**（`main.jsx:108/114`），以及 `workbenchEvents.js` 里 `LEGACY_CHAT_KINDS`（`chat_chunk`/`message_presentation`/`message`/`question`）对应的旧投影与 `main.jsx` 里的 `ChatMessage`/`QuestionCard`/对话切换器/回收站。

**因此顺序必须是：先把"提案确认"搬进 Agent 面板（也就是把 9.2(d) 的 `proposalState.js` 接线），再拆开关。** 反过来做，会让提案在所有面板上都不可确认——比现在更糟。旧投影的退役可以按既有纪律走"先 quiesce 调用者 → 只读审计 → 再删"（见用户既有反馈条目）。

### 9.5 本轮核查的边界（必须转述给用户，别扩大成结论）

- **无法从存储状态复现用户的原症状**：生产库 `K:\Bobbytagging\Data\workbench.db` 有 3 条对话、3 个 agent 会话、**0 条提案、0 条 `active_chat_turn` 标记、`agent_event` 表为空**，最新一条审计动作是 `2026-09-13T10:20`。所以 §9.2/§9.3 是**源码级根因链 + 本地库结构佐证**，不是"已在生产复现"。用户报的现象需要用他当时那个数据集现场复现一次再改。
- 本轮的定位**没有依赖任何运行时探针**，全部是代码路径 + 只读库结构。
- §9.2(d)、§9.3 的"缺 `dataset_id`"、§9.4 的守卫测试，都是**待办依据**，不是已修项。

### 9.6 副作用披露（我做只读探针时产生的，非业务数据）

用 `sqlite3 ... mode=ro` 读了两套库，读取本身被 SQLite 允许，但**会在目录里创建/触碰 sidecar 文件**：

- `K:\Bobbytagging\Data-Test\workbench.db-wal`（0 字节）与 `workbench.db-shm`（32768 字节）在 2026-09-22 21:44 被创建——**此前不存在**。
- `K:\Bobbytagging\Data\workbench.db-shm` 的 mtime 被推到 2026-09-22 21:45:36。
- **业务数据零变化**：生产目录文件数仍为 537；`workbench.db-wal` 仍是 103032 字节 / Sep 17 07:19；`workbench.db` 仍是 13017088 字节 / Sep 17 07:18；Data-Test 的 `-wal` 为 0 字节（无未落盘事务）。
- 处置建议：`Data-Test` 是测试目录，那两个空 sidecar 可留可删；生产目录只动了 `-shm` 的 mtime，**未做任何写入**。后续如需再查库，改用**只读副本**而不是直连生产库。

---

## 10. 需要你裁决的三件事

1. **是否推送当前这 5 个未推送提交**（`8fb7fda` `0d653fc` `5c189ce` `3cc9df2` `0fbbc25`）。推送需用户逐次授权，不由你或执行方自行决定。
2. **是否把 §9 的两个缺陷 + 合并要求排成下一轮**。若排，建议的轮次边界（**先不要一次全做**）：
   - 轮 A（必须最先，其余都等它）：把 `proposalState.js` 接进 Agent 面板，让"待确认提案 + 待办任务"按 `dataset_id` 呈现，确认/执行/抽样/撤回按钮随之落位。这一轮同时把 9.2(b) 的 `conversation_id` 依赖去掉。
   - 轮 B：`dataset_id` 契约扩展（`bridge.status()` / `agent_application.status()` 补字段）+ 前端守卫：会话数据集 ≠ 当前数据集时不恢复，并给出明确的一次性提示。属低层契约变更，需 red test。
   - 轮 C：拆掉新旧切换（含改写 `panelDefault.test.js`），旧投影按 quiesce 纪律退役。
   - 每轮都要先在**用户当时的数据集**上复现一次原症状（§9.5：生产库当前 0 提案、`agent_event` 为空，不能只靠源码推断验收）。
3. **下一轮先做 §6 的哪一项**（若优先修缺陷，此项顺延）：6.1 安装器专项（最大、需独立一审）、6.2 provider 引导（阻塞标签提案卡片的人工验证）、6.3 `source_sha256` 扩范围（低层契约，需 red test）、6.4 源码直跑路径退役、或先补 `.agent-team/` 到 `.gitignore` 的小提交。

---

## 11. 2026-09-23 定时唤醒会话报告：Phase 0 未通过 → 全轮未执行

**结论 `BASELINE_MISMATCH`。Phase 0 第 4 项失败；按 §3 Phase 0 的「任一不过就停，别硬来」，本轮未做任何提交、未改任何源码。**

### 11.1 环境自检逐项

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | `codex.exe login status` | ✅ `Logged in using ChatGPT` |
| 2 | Codex 额度探测（只读、极小） | ✅ 已恢复。见 §11.6 |
| 3 | `qoderclicn.exe status` | ✅ `Username: aliyun7882947603` |
| 4 | `git status` 与 §1 基线 | ❌ **不一致** |

第 3 项的附带噪音（既存环境问题，与本次无关）：`Skipped invalid MCP server "sorftime"`（枚举值 `streamableHttp` 不被接受，应为 `streamable-http`）与 `NODE_TLS_REJECT_UNAUTHORIZED=0` 警告。

第 2 项的事件流里除 `turn.completed` 外另有一条 `item.completed / item.type="error"`，内容是 `Skill descriptions were shortened to fit the skills context budget…`——**非致命提示，不是额度/限流错误**，不要据此判 `QUOTA_EXHAUSTED`。

### 11.2 Phase 0 第 4 项的实测差异（本轮唯一阻塞点）

§1 声明的基线是「`ahead 5` + 未跟踪 `docs/CODEX_HANDOFF.md`」。逐条实测：

| 项 | §1 声明 | 实测 | |
| --- | --- | --- | --- |
| HEAD | `0fbbc255e561322bccf39e7a22de576b7779431e` | 同 | ✅ |
| ahead/behind | `ahead 5` | `git rev-list --left-right --count origin/feature/0.5.0-delivery...HEAD` → `0	5` | ✅ |
| 未跟踪 `docs/CODEX_HANDOFF.md` | 是 | 是 | ✅ |
| **其他工作区状态** | **未提及** | **82 个已 staged 文件：`82 files changed, 125503 insertions(+), 6 deletions(-)`** | ❌ |

### 11.3 这 82 个文件是什么（不是垃圾，是在建特性）

一个完整、连贯、自带设计文档的在建特性「**关键词调研**」——工作台的第二个顶级页面（顶栏「关键词调研」入口 /「返回商品打标」退回，与打标并列）。

- 文件 mtime 全为 **2026-09-23 00:24–00:30**（`.gitignore` 00:24 / `backend/app.py` 00:27 / `frontend/src/main.jsx` 00:30），**晚于** `docs/CODEX_HANDOFF.md`（09-22 21:47）。
- 即：它是交接文档写完之后、由另一次会话或手工工作 staged 进来的，与 §1 基线快照不是同一时刻的状态。
- 分组：`backend/keyword_research/**` 35、`tests/fixtures/keyword_research/**` 32、`frontend/src` 5（`keywordResearch.jsx` / `keywordResearch.css` / `keywordState.js` + `main.jsx`、`settingsTools.jsx` 修改）、`frontend/tests` 3、`tests/test_keyword_research_{run,pipeline,matrix_switch}.py` 3、`backend/app.py`、`backend/storage_management.py`、`.gitignore`、`docs/关键词调研接入.md`。

### 11.4 为什么这构成阻塞（不是「描述不全」那么轻）

1. **任何普通 `git commit` 会把它们一起提交。** git 提交的是整个 index。Phase 1/2/3/4 要求「一轮一提交 + 提交前核对文件集合」（约束 11/12）；在此 index 上执行 `git add docs/CODEX_HANDOFF.md && git commit` 会产出一个混入用户未完成特性的提交。
2. **计划关键假设为假：Phase 3 要改的文件已经被改过。** `frontend/src/main.jsx` 的工作区版本相对 HEAD 已有改动，且位置与 Phase 3 施工点**完全重叠**：
   - 新增 `const [page,setPage]=useState(PAGE_HOME)` 紧挨 `agentMode`（Phase 3 的挂载点）；
   - 整条 `return <div className={'workbench '...}>`（含顶栏、`{agentMode?<AgentPanel did={did}/>:<>` 三分支、§9.2(a) 的确认 UI 分支）被重写；
   - `{modal&&` → `{modal&&page===PAGE_HOME&&`。

   同时 `frontend/tests/panelDefault.test.js` 也已被改（新增 2 条 keyword 页断言）。
3. **推论**：轮 A 的完成判据「`npm test` 全绿」在这个工作区里**无法被定义为按 §1 基线绿**——它测的是混合树；§10 要求的「实际命令与计数」会失真。

### 11.5 顺手核实的三处事实（全部只读，供下一轮直接用）

- **§7 关于产物路径的说法确实是错的**（本轮任务 §4 的判断成立，已读源码确认）：
  - `delegate.py:118 validate_external_state_root()` 明确 `raise ValueError("state root must be outside the repository and Git common directory")`；
  - 默认 state root 为 `Path.home()/".codex"/"agent-team"` → **`~/.codex/agent-team`**，**不是** `<repo>/.agent-team/runs/<run_id>/`。
  - → §7 那条「把 `.agent-team/` 加进 `.gitignore`」的建议基于错误前提，应连同命令一起改写。
- **§7 的委派命令不完整**：`argparse` 实际还有 `--allow-path`(append)、`--reasoning-effort`、`--state-root`，以及整套 plan 审批流 `--prepare-plan` / `--approved-plan` / `--approved-plan-sha256` / `--plan-approval`（另有 `--mark-review` / `--integrate-run` / `--recover-run` 等回传/整合开关）。文档只写了 `--repo/--task-file/--model`。
- **Phase 3 的前提成立**：全 `frontend/` grep `proposalState|workbenchActions|proposalView|taskView|selectPending`，`frontend/src/` 下只命中 `proposalState.js` 自身，其余全在 `frontend/tests/proposalState.test.js` → 确认**零生产消费者**。

### 11.6 Codex 用量

仅 **1 轮**，且只是 Phase 0 的只读额度探测（不是方案轮）：

```json
{"type":"turn.completed","usage":{"input_tokens":22675,"cached_input_tokens":11648,"cache_write_input_tokens":0,"output_tokens":12,"reasoning_output_tokens":0}}
```

### 11.7 测试证据（在**当前脏树**上跑，≠ 基线口径）

- 前端：`cd frontend && npm test`（= `node --test "tests/**/*.test.js"`）→ **`tests 97 / pass 97 / fail 0 / duration_ms 227`**。§2 基线是 67——差额 30 条来自 staged 的 keyword_research 测试（`keywordState.test.js`、`keywordStreamContract.test.js`）。→ 这份在建工作在前端侧是**绿**的。
- 后端：`.venv/Scripts/python.exe -m pytest -q` → **`626 passed in 207.29s (0:03:27)`**，exit 0，无 `F`/`E`。§2 基线是 598——差额 28 条来自 staged 的 keyword_research 后端用例（`tests/test_keyword_research_{run,pipeline,matrix_switch}.py`）。→ 这份在建工作在**前后端两侧都是绿的**。
- ⚠️ **口径声明**：以上计数的被测对象是「`0fbbc25` + 82 个 staged 文件」，**不是 §1 基线**，不可与 §2 的 598/67 直接等同解释。

### 11.8 Phase 执行结果汇总

| Phase | 结果 | 理由 |
| --- | --- | --- |
| 0 环境自检 | ❌ 失败 | 第 4 项 git status 与 §1 基线不一致（多出 82 个 staged 文件） |
| 1 修正 CODEX_HANDOFF §7 | ⏭️ 跳过 | Phase 0 未过；且任何提交都会污染用户在建工作。三条判据事实已核实（§11.5） |
| 2 固化桥接为 Qoder 技能 | ⏭️ 跳过 | 同上；`SKILL.md` **未创建** |
| 3 缺陷轮 A | ⏭️ 跳过 | 施工点 `main.jsx` 已被在建改动覆盖，无法在 §1 基线上定义「轮 A 绿」 |
| 4 缺陷轮 B | ⏭️ 跳过 | 依赖轮 A |

### 11.9 提交与 git 对比

- **提交 0 个。** 起始与结束的仓库状态完全一致：HEAD `0fbbc255e561322bccf39e7a22de576b7779431e`、`ahead 5`(origin `0`)/behind `0`、index 仍为那 82 个文件、未跟踪仍只有 `docs/CODEX_HANDOFF.md`。
- 本报告的写入对象是**未跟踪文件**，不进 index，因此不改变上述任何一项（已核实：未执行任何 `git add`）。

**本轮产物（全部在仓库之外）**

| 文件 | 绝对路径 |
| --- | --- |
| 探测任务契约 | `C:\Users\Administrator\AppData\Local\Temp\gewu-bridge-20260923\probe-task.md` |
| 事件流 | `C:\Users\Administrator\AppData\Local\Temp\gewu-bridge-20260923\probe-events.jsonl` |
| Codex stderr（空） | `C:\Users\Administrator\AppData\Local\Temp\gewu-bridge-20260923\probe-stderr.txt` |
| 方案输出 | `C:\Users\Administrator\AppData\Local\Temp\gewu-bridge-20260923\probe-plan.md`（内容 `17 + 25 = 42.`） |
| Phase 2 目标（**未创建**） | `C:\Users\Administrator\.qoder-cn\skills\codex-bridge\SKILL.md` |

（`~\.qoder-cn\skills\` 当前只有 `excel-analysis` / `markdown-converter` / `quickbi-smartq-chat` / `zimage-prompt-expander`。）

### 11.10 用户白天怎么亲手验证

**第一步 · 确认真实状态（只读，不需要重装任何东西）**

```bash
cd "D:\数据打标项目"
git status --short --branch            # 预期：## feature/0.5.0-delivery...origin/...[ahead 5]
                                       #       82 行 staged(M/A)、1 行 "?? docs/CODEX_HANDOFF.md"
git diff --cached --shortstat          # 预期：82 files changed, 125503 insertions(+), 6 deletions(-)
git rev-parse HEAD                     # 预期：0fbbc255e561322bccf39e7a22de576b7779431e
git rev-list --left-right --count origin/feature/0.5.0-delivery...HEAD   # 预期：0	5
```

**失败判定**：若 `git diff --cached --shortstat` 输出为空（staged 为 0），说明这批文件已被处理掉，本报告的阻塞**不成立**，直接重跑本任务即可。

**第二步 · 选择解锁方式（三选一，由你决定）**

- **(A) 推荐** —— 把「关键词调研」当作它**自己的一个提交**落盘（消息用你的原话），让工作区回到 §1 声明的基线（只剩未跟踪的 `CODEX_HANDOFF.md`）。之后重跑本任务，Phase 1→4 可原样执行。
- **(B)** 若这批工作**还没做完** —— `git stash push -u -m "keyword-research WIP"`。**注意**：stash 会把 staged 内容一并带走，且本任务要改**同一个 `main.jsx`**，任务结束后 `git stash pop` 大概率冲突，需手工解冲突。
- **(C)** 坚持「就在这个脏树上做 Phase 3」 —— **不建议**；即使接受轮 A 的「绿」为混合树口径，仍须先解决第(1)点 index 污染（否则提交会带上这 82 个文件）。

**第三步 · 无论选哪种，Phase 1 那条修补都要做**：按 §11.5 改写本文档 §7 —— 产物落 `~/.codex/agent-team/{plans,runs}/<id>/`（仓库外），删除「把 `.agent-team/` 加进 `.gitignore`」的建议，并把委派命令补全为 `--repo/--task-file/--model/--allow-path/--reasoning-effort/--state-root` 与 `--prepare-plan` 审批流。

### 11.11 未完成项与阻塞点

- **阻塞**：§11.2 的 82 个 staged 文件（Phase 0.4）。
- **未完成**：Phase 1、2、3、4 全部。
- **未验证**：轮 A 的浏览器级验收（约束 21）；轮 B 的确定性 red test 与变异（非空转）检查——均未开始。
- **顺带提醒**：Codex 额度本轮已恢复，但那是**当时点事实**；长任务期间仍可能再次耗尽，届时按 §9 停止条件报 `QUOTA_EXHAUSTED`。
