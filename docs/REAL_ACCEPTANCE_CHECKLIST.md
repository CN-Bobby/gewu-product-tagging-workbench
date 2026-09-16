# Real Acceptance Checklist（M6 收口后，下一阶段执行）

状态：**尚未执行**。本文件只定义“怎么验收、看什么证据、什么算通过”，不包含任何已完成的验收结论。

来源：M6 Runtime Hardening 收口（`PROJECT_HANDOFF.md` §5.5）。适用对象是**本地分支源码**（截至 `3257e50`，后端全量 596 passed），不是本机安装的 `0.5.0-beta.6+2f7bf2b59975` 候选包——那个包不含 M6 及之后的任何修复。

## 0. 前置条件与硬约束

执行前必须确认：

- [ ] 工作台已停止（`workbench.db` 带 WAL，运行中复制不一致）；需要备份时先停再备份。
- [ ] 使用**隔离数据目录**（`TAG_STORAGE` 指向临时/验证目录）+ 隔离的 `TAG_PROVIDER_HOME`；不要用 `K:\Bobbytagging\Data` 的正式库做实验。
- [ ] 正式 310 行数据与 17 个人工值**不得被修改**；如需触碰正式库，只读。
- [ ] 真实模型调用会消耗额度：执行前逐段向用户申请授权，并说明预计请求量。
- [ ] 不把完整 Key 写入文档、日志、Git、任务证据或错误详情。
- [ ] 不创建 tag/Release、不标记 `accepted`、不推送远端（推送需逐次授权）。
- [ ] 任何一步出现“看起来成功但无法给出证据”的情况，按失败记录，不写成通过。
- [ ] 明确不做：per-session Reasonix home、credential versioning、DB/schema 变更、deferred backlog 清理（见 `PROJECT_HANDOFF.md` §11，均 NON-BLOCKING）。

建议先跑一次离线门禁，确认验收起点干净：

```powershell
.venv\Scripts\python.exe -m pytest -q            # 期望 596 passed
.venv\Scripts\python.exe scripts/repository_audit.py
```

## A. UI Agent（Agent 面板）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| A1 | 新建 session（面板不选模型，即默认路径） | `POST /api/agent/sessions` 200；`start` 200；响应/状态里 model 明确 | 待执行 |
| A2 | 连续两轮对话 | 两轮都 `outcome=completed`；事件流里 `message_started → … → turn_settled → message_completed` 顺序正确；第二轮能读到第一轮的上下文 | 待执行 |
| A3 | 触发一次 tool / MCP 调用（例如读工作表或路由 Skill） | 事件流出现 `tool_call_started` / `tool_call_settled`；被拒能力（如 `run_skill`）只作为诊断出现，不当作成功 | 待执行 |
| A4 | question interaction（native ask） | 面板出现问题卡；回答后 `question_settled` 落库；回合继续并正常结束 | 待执行 |
| A5 | explicit cancel（回合中途取消） | `cancel` 返回 `cancel_requested=true`；回合以 cancelled 结束；**会话仍为 active**，不是 failed | 待执行 |
| A6 | cancel 后继续下一轮（含 interrupted-turn recovery） | 下一轮能正常完成；`/api/agent/sessions/{id}` 的 `remote_session_id` 与取消前**相同**（同一 native session resume，不是新建会话） | 待执行 |
| A7 | 明确“不允许”的越权尝试（如让 Agent 跑 shell / 读文件） | 明确拒绝并可解释；不出现绕过工作台 MCP 的数据访问 | 待执行 |

不打算在验收里复现的：90 秒级真实 MCP 卡死（由 `tests/test_execution_budget.py` 的确定性 peer 覆盖）。

## B. Execution Budget（只核对配置与投影，不真等 120/180/300 秒）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| B1 | 读源码常量，确认三套数值 | UI=120/180/360/1200；Worker=180/300/360/1200；Coordinator=120/240/360/1200 | 待执行 |
| B2 | 确认预算由调用点显式注入 | `agent_application.UI_EXECUTION_BUDGET`、`jobs.WORKER_EXECUTION_BUDGET`、`jobs.COORDINATOR_EXECUTION_BUDGET` 各在对应调用点；`ReasonixLaunchConfig.budget` 默认仍为 `None` | 待执行 |
| B3 | 确认传输层上限是推导出来的 | wall-clock(1200) + `BUDGET_TRANSPORT_GRACE_SECONDS`(60) = 1260，不是硬编码 | 待执行 |
| B4 | 触发一次真实语义超时并确认 HTTP 结构化原因 | 502 + `detail.code == 'turn_error'` + `detail.reason` 为机器可读的超时码（如 `turn_idle_timeout`）；普通 `TurnError` 不出现 `reason` | 待执行 |
| B5 | 确认 MCP 单次上限仍是独立一层 | 生成的 `reasonix.toml` 中 `call_timeout_seconds = 300` | 待执行 |
| B6 | 超时/取消后会话可用性 | 超时后会话仍可再次 `send`（或按契约回收）；不出现无法恢复的 busy 卡死 | 待执行 |

B1–B3、B5 可离线完成（`tests/test_budget_policy.py` 已覆盖）；B4、B6 是真实运行时观察。

## C. Settings lifecycle（模型 / 凭据）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| C1 | active Agent 存在时改 model / 切 primary / 禁用 / 删除 Key | 全部 **409** `detail.code == 'settings_in_use'`；配置文件字节未变 | 待执行 |
| C2 | 停止 Agent 后重复 C1 | 变更成功（200）；不再返回 409 | 待执行 |
| C3 | 新建 Agent | 使用**新**配置；旧会话即使恢复也仍用创建时的 model/provider/credential | 待执行 |
| C4 | 同 dataset 开第二个不同 model 的会话 | **409** `detail.code == 'runtime_config_in_use'`；已有会话的 dataset home 未被改写 | 待执行 |
| C5 | 悬空 / 停用凭据 | 明确失败（`credential_unavailable`），**不**回退到当前 primary | 待执行 |
| C6 | 凭据删除后的磁盘残留 | 记录：删除后旧 dataset home 的 `.env` 可能残留该 secret，直到下一次 `environment()` 重写（已知限制，不算失败） | 待执行 |

C1–C6 的确定性版本已由 `tests/test_model_credential_lifecycle.py`（T1–T11）离线覆盖；本节是在真实部署上复核同一契约。

## D. Jobs / Batch（只验收当前可触达路径）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| D1 | 持续草稿里跑一次真实的直接模型执行（batch 路径） | 任务完成或按预期部分完成；`ModelAttempt` 记录齐全；失败项有明确原因，不静默丢失 | 待执行 |
| D2 | 反推（source label inference）跑一次小样本 | 产出可审核草案；截断但有可用结果时**不整轮作废**；失败时也有程序基线与来源说明；选图片证据时 `vision_requests > 0` | 待执行 |
| D3 | 批量执行期间的 Settings 变更 | 返回 409（batch 也纳入统一锁） | 待执行 |
| D4 | **不验收**：休眠的 legacy worker（`POST /api/datasets/{id}/runs`）与 legacy coordinator（`jobs.start_chat`）入口 | 其代码与契约正确性由测试保证，**不为验收强行复活这些入口** | 不适用 |

## E. Process lifecycle（进程归属）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| E1 | 正常工作台关闭（停止服务/退出程序）后观察进程 | Agent 会话收到正常 stop；owned 的 `reasonix` 进程随之消失（只观察，不按映像名杀进程） | 待执行 |
| E2 | 重启工作台后新建 Agent | 能正常创建并完成一轮；不残留上次会话的进程/锁 | 待执行 |
| E3 | 父进程被硬杀的回收能力 | **不要求为了验收故意 hard-kill 主程序**：已有稳定自动证据（`tests/test_agent_runtime.py` 的 Job Object 用例，含“关闭最后一个 job 句柄即结束不读 stdin 的子进程”“整棵树被覆盖”）；如需人工复核，用隔离实例进行 | 用自动证据 |
| E4 | 宿主不支持 Job Object 时的降级 | 有 `job_unavailable` 事件与 `diagnostics()['job']` 事实，不静默失败（自动证据同上） | 用自动证据 |

## F. Persistence（重启后可用性）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| F1 | 重启后读取基本数据 | 数据集、工作表、草稿、已确认标签、任务记录仍可读取且一致 | 待执行 |
| F2 | 历史查看面不受影响 | 历史对话列表、消息、问题历史、附件仍可打开；不因 M5/M6 退役而丢失 | 待执行 |
| F3 | 启动时的恢复动作 | 启动日志/状态显示 running→interrupted 等恢复行为符合预期，不产生新的“运行中”假象 | 待执行 |
| F4 | 升级/回退路径 | 按 `docs/UPGRADE_ROLLBACK.md` 演练一次；回退需要“旧程序 + 对应升级前数据备份”，不能只替换程序 | 待执行 |

## G. Regression（回归门禁）

| # | 步骤 | 通过判据 | 记录 |
|---|---|---|---|
| G1 | 后端全量测试 | `python -m pytest -q` → **596 passed**（2026-09-17 最近一次真实全量；本轮若有代码改动必须重跑） | 已具备（596 passed） |
| G2 | 仓库审计 | `scripts/repository_audit.py` → `forbidden_paths` / `secret_hits` / `sample_hits` 均为空 | 已具备 |
| G3 | 前端构建 | 若最终交付前**有任何前端改动**→ 必须重跑 `npm run build --prefix frontend`；否则沿用最近一次全绿结果 | 待确认 |
| G4 | M6 专项套件 | `tests/test_agent_runtime.py`、`tests/test_agent_http.py`、`tests/test_execution_budget.py`、`tests/test_budget_policy.py`、`tests/test_model_credential_lifecycle.py` 全绿 | 已具备 |

## 通过 / 不通过的判定

满足以下全部条件才算真实验收通过：

- A、C、D、F 的每一行都有**可复现的证据**（命令输出、事件序列、HTTP 响应体或截图），不接受“看起来正常”；
- B 的三套数值与 HTTP 结构化 reason 在真实运行时被观察到；
- E 的进程回收结论来自自动证据或隔离实例，而不是在主实例上做破坏性试验；
- G 全绿，且交付前若改了前端就重跑前端构建；
- 所有真实模型调用都在授权范围内，且未触碰正式 310 行数据与 17 个人工值。

通过之后才进入交付：构建**新**候选（不要覆盖 beta.6 产物）、记录 commit / build ID / SHA256、包内冒烟、本机覆盖升级、更新 `CHANGELOG.md` / `RELEASE_LEDGER.json` / 交付台账 / 验收记录，并保留回退点。

## 证据记录表（执行时逐行填写）

| 段落 | 执行日期 | 复核人 | 结论 | 证据位置 |
|---|---|---|---|---|
| A | | | | |
| B | | | | |
| C | | | | |
| D | | | | |
| E | | | | |
| F | | | | |
| G | | | | |
