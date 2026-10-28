# 格物工作台 DEV / TEST / PROD 运行规则

本手册是跨对话复用的环境规则。它说明如何识别环境、保护数据和理解真实外部调用授权，不替代当前运行状态检查。

## 1. 每次工作的事实来源

每个新任务仍须按根目录 AGENTS.md 检查 Git 状态、分支、HEAD 和上游，并阅读交接、版本及相关模块文档。涉及运行程序时，再通过桌面入口或 Run Center 查询当前状态。版本文件、旧截图、旧聊天记录和本手册都不能证明某进程正在运行什么代码。

Run Center 配置通常位于：

    %LOCALAPPDATA%\GeWuWorkbench\RunCenter\config.json

查看当前 DEV 状态：

    powershell -NoProfile -File "$env:LOCALAPPDATA\GeWuWorkbench\RunCenter\run-center.ps1" -Profile DEV -Action Status

查看 TEST 状态：

    powershell -NoProfile -File "$env:LOCALAPPDATA\GeWuWorkbench\RunCenter\run-center.ps1" -Profile TEST -Action Status

以实时输出为准记录环境名、页面/API 地址、代码来源、commit、build ID、dirty 状态、数据/缓存路径、外部调用 policy 和实例 ID。状态缺失、端口冲突、目录重叠或身份不一致时，停止操作并先查明原因。

DEV/TEST 后端必须提供只读 `GET /api/runtime/status`，至少返回 `profile`、`instance_id` 和 `external_effects.policy`。Run Center 按此接口核对本次启动的环境与策略；源码变更后应先通过接口回归，再由 Run Center 启动并打开页面。该接口报告进程环境中选择的策略，不代表对所有第三方请求做了网络级拦截。

## 2. 环境职责

| 环境 | 用途 | 代码形态 | 数据规则 |
| --- | --- | --- | --- |
| DEV | 观察正在开发的代码、热更新和短周期验证 | 当前 Run Center 配置指向的开发工作树；具体路径每次读取配置确认 | 独立 DEV 数据与缓存 |
| TEST | 人工验收一个确定的结果 | 从明确 commit 生成的冻结候选；验收中不得热更新 | 独立 TEST 数据与缓存；重置前备份并核对目标路径 |
| PROD | 实际业务使用 | 已部署的稳定程序 | 真实业务数据；禁止作为测试、迁移演练和重置目标 |

本机历史配置曾使用 DEV 页面 5174/API 8896，TEST 8895，PROD 8765，以及 K:\GeWuDev、K:\GeWuTest、K:\Bobbytagging 下的数据目录。这些是定位线索，不是永远固定的事实；每次以当前 Run Center 配置和 health/runtime status 为准。

每个页面都应核对它实际连接的后端。切换分支或浏览器 URL 不会自动改变已运行后端；两个不同页面也可能连到同一个 API。

## 3. 外部调用和授权

1. DEV/TEST 通过桌面入口启动时默认允许外部调用且不弹出模式选择。自动化测试、浏览器模拟和候选冻结检查仍须使用假 provider 或网络拦截；需要显式离线实例时使用 Run Center 的 `-ExternalEffectsMode offline`。
2. 允许外部调用不代表任务自动获得业务数据或用量上限。模型、MCP、SellerSprite、Sorftime 和 Amazon 请求可能产生费用，且当前策略不强制请求次数、token 或金额上限。
3. 自动化、浏览器模拟和候选冻结脚本必须在隔离数据上运行；如需实际调用，先检查目标项目、输入范围和预期服务，任务完成后记录可取得的请求、用量和产物信息。
4. DEV/TEST 的运行策略由 Run Center 启动参数设置。切换已运行实例策略时，按 Run Center 身份停止后重新启动；不得结束任意 PID、手工继承环境变量或直接改运行中的进程。
5. PROD 的真实业务数据与凭据保持独立，不得用 DEV/TEST 的测试报告声称 PROD 已验收。

“一个任务”不等于“一次请求”或固定费用。Reviews 可能对每个 ASIN 发起重复采样并调用翻译模型；VOC 和关键词分析包含多阶段请求；寻龙诀可能产生多个树/趋势请求。启动真实任务前须查看模块自己的预算和请求规则；无法给出可靠上限时用离线模拟。

## 4. 凭据、配置和数据

- 每次只通过运行状态检查凭据是否“已配置/未配置”，不读取或回显 Key。
- DEV/TEST 是否共享当前 Windows 用户的 DPAPI 凭据库、PROD 是否使用独立凭据，必须从当前实例配置和运行状态核实；不能从历史对话推断。
- 模型选择等业务配置可以按环境独立。任务创建后应使用其冻结的模型/凭据身份，不因后续设置变化悄悄改换 Key。
- 凭据、数据库、Reasonix 会话、任务检查点和业务数据不是同一个目录。验证其中一项时不得覆盖或迁移其他项。
- 所有自动和人工测试使用合成数据或经核准脱敏的副本。正式数据只读保护；测试复位、回滚和恢复操作必须限定在 TEST 目标路径并留有备份证据。

## 5. 新对话执行清单

遇到新的开发/验收任务，按以下顺序：

1. 阅读根目录 AGENTS.md、PROJECT_HANDOFF.md、docs/RELEASE_GOVERNANCE.md、VERSION.json、RELEASE_LEDGER.json 和 CHANGELOG.md。
2. 阅读本手册与当前模块的验收/交接文档；全模块测试还要读 docs/ALL_MODULES_END_TO_END_TEST_PLAN.md。
3. 检查当前 Git 和目标 worktree，保护已有未提交改动。
4. 查询 DEV/TEST 的实时身份，确认实际代码和数据路径。
5. DEV/TEST 桌面入口默认允许外部调用；是否发起具体真实任务仍按当前用户指令执行。Run Center 的启动策略不是任务白名单，也不限制请求次数或费用。
6. 分别报告自动化、浏览器、真实接口、人工业务语义、安装/升级/回退的结果；没有证据的门禁标记为未验收。

同一仓库中新开的 Codex 任务应以 AGENTS.md 为固定入口，但项目无关对话、另一个 checkout/分支或未同步的远端分支未必包含这些文件。需要跨机器或跨分支一致时，须按用户授权把规则提交并同步到共享仓库。
