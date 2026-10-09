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

## 2. 环境职责

| 环境 | 用途 | 代码形态 | 数据规则 |
| --- | --- | --- | --- |
| DEV | 观察正在开发的代码、热更新和短周期验证 | 当前 Run Center 配置指向的开发工作树；具体路径每次读取配置确认 | 独立 DEV 数据与缓存 |
| TEST | 人工验收一个确定的结果 | 从明确 commit 生成的冻结候选；验收中不得热更新 | 独立 TEST 数据与缓存；重置前备份并核对目标路径 |
| PROD | 实际业务使用 | 已部署的稳定程序 | 真实业务数据；禁止作为测试、迁移演练和重置目标 |

本机历史配置曾使用 DEV 页面 5174/API 8896，TEST 8895，PROD 8765，以及 K:\GeWuDev、K:\GeWuTest、K:\Bobbytagging 下的数据目录。这些是定位线索，不是永远固定的事实；每次以当前 Run Center 配置和 health/runtime status 为准。

每个页面都应核对它实际连接的后端。切换分支或浏览器 URL 不会自动改变已运行后端；两个不同页面也可能连到同一个 API。

## 3. 外部调用和授权

1. DEV/TEST 默认离线。自动化测试、浏览器模拟和候选冻结检查都必须清除继承的 live-validation 标记，并使用假 provider 或网络拦截。
2. 真实模型、MCP、SellerSprite、Sorftime 和 Amazon 网络调用只能在用户对具体模块和本轮任务明确授权后进行。已保存凭据不等于本轮授权。
3. 当前 Run Center 的“真实接口验收”选择只设置当前 DEV/TEST 后端进程的外部调用策略。选择开启会开放这个进程里所有受门禁管理的外部服务；它不能只开放一个模块、单个任务或某个 Key，也不强制请求次数、token 或金额上限。
4. 因为门禁不是任务级限额，启动前要确保该实例没有其他用户或自动任务会触发外部服务；只执行已授权的目标任务。运行范围无法预测、用量不可核对或请求超出授权时，不启动/继续真实任务。
5. 改变 DEV/TEST 的离线/真实调用策略须按 Run Center 身份停止旧实例，再重新启动并选择模式。不得通过结束任意 PID、手工继承环境变量或直接改运行中的进程来切换策略。
6. 真实运行报告记录 Run ID、构建、服务种类、输入范围摘要、请求账、可取得的 token/费用及产物 SHA-256。无法取得用量写为 unknown；不得记录密钥或完整敏感清单。任务完成后关闭本次 live-validation 实例。
7. PROD 的真实业务权限不应被 DEV/TEST 的临时授权覆盖，也不得使用 DEV/TEST 的测试报告声称 PROD 已验收。

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
5. 默认只做离线测试。真实外部任务在用户明确授权前不启动；Run Center 的进程级“是”不能替代具体任务授权或被说成预算上限。
6. 分别报告自动化、浏览器、真实接口、人工业务语义、安装/升级/回退的结果；没有证据的门禁标记为未验收。

同一仓库中新开的 Codex 任务应以 AGENTS.md 为固定入口，但项目无关对话、另一个 checkout/分支或未同步的远端分支未必包含这些文件。需要跨机器或跨分支一致时，须按用户授权把规则提交并同步到共享仓库。
