# Reasonix 集成架构审计与最小化重构方案

审计对象:`D:\数据打标项目` 当前 `feature/0.5.0-delivery`(commit `57ec211`)。本文只做审计与方案,不含代码改动。

---

## 1. 完整调用链:从 UI 输入到最终响应

```
[1] 用户点“发送”
    frontend/src/main.jsx  send()
        POST /api/datasets/{did}/chat  {text, model, vision_model,
              text_credential_id, vision_credential_id, proposal_id,
              conversation_id, attachment_ids}
              │
[2] backend/app.py:267  chat()  →  conversation.start(...)
              │
[3] conversation.start()                       ← 同步、立即返回
      · chat_threads.require_thread()          取/建网页对话
      · 写 WorkbenchMeta('chat-turn:<turn_id>')  turn → conversation 映射
      · 插 user Message + 一条 status='running' 的 assistant Message
      · 写 WorkbenchMeta('active-chat:<did>')   {turn_id, message_id, conversation_id}
      · draft.emit('message')                  落 draft_event 表
      · threading.Thread(run, ...)             后台线程跑回合
      返回 {message_id, turn_id}
              │
[4] conversation.run()                         ← 后台线程，真正干活
      (a) 复用/重建 ACP 客户端
          clients[did] 内存字典；fingerprint = sha256(model, vision_model,
          text/vision_credential, instruction_hash(), reasonix_baseline)
          对话切换 / 凭据变化 / instruction_hash 变化 → client.close() + park runtime
      (b) ACP.__init__ → subprocess.Popen([reasonix, 'acp', '--model', model],
              cwd=runtime_dir(...), env=environment(...))
          + initialize → session/new 或 session/resume(+reloadExtensions)
      (c) client.prompt(handoff_context + text)   ← 阻塞直到本回合结束
      (d) on_event 回调 → 写 Message.text / Message.events
                        → 重算 presentation
                        → draft.emit(...) 落 draft_event 表
      (e) 成功 → Message.status='completed'
          HarnessError → status failed/interrupted + error_code + technical
      (f) finally: 清 active-chat、释放 busy[did]、补投排队中的回答
              │
[5] 前端经 SSE 收流
      EventSource /api/drafts/{did}/events?after=<cursor>
        app.py:405 draft_events  →  每 0.5s 查 draft_event 表（>cursor, limit 200）
                                 →  yield  id:/data:  帧
        main.jsx:53-58  按 kind 归并：chat_chunk / message_presentation /
                        message / question / job / proposal / version / cell ...
```

要点:**没有实时推流**。Agent 的正文、工具状态、进度全部先落 `draft_event` 表,前端再以 0.5s 轮询拉取。所以“流式”的实际粒度是 0.5s + 一次 SQLite 查询。

## 2. 涉及的文件与模块

| 层 | 文件 | 职责 |
|---|---|---|
| UI | `frontend/src/main.jsx` | 发送、SSE 归并、中止、提问卡、模型/Key 选择 |
| UI | `frontend/src/sourceLabelTools.jsx`、`workbenchTools.jsx` | 反推定义、字段用途 |
| 路由 | `backend/app.py`(420 行 / ~70 条路由) | HTTP + SSE + 静态托管 + `/api/server/stop` |
| 会话编排 | `backend/conversation.py`(327 行) | `clients{}`、`busy{}`、`run()`、`start()`、`stop()`、`answer()`、`_resume_queued()`、`_handoff_context()` |
| CLI 客户端 | `backend/harness.py`(501 行) | `ACP` 类：Popen、JSON-RPC、`_read`、`_stderr`、看门狗、错误归类、`runtime_dir()`、`system_prompt()` |
| 隔离与凭据 | `backend/runtime_isolation.py`(93 行) | 隔离 home、config.toml、`.env` 密钥、session 文件迁移+备份、icacls |
| 展示 | `backend/conversation_presentation.py` | 正文/操作记录切分 + progress |
| 业务问答 | `backend/business_questions.py` | `ask` 落库、`answer` 排队续跑 |
| 对话/元数据 | `backend/chat_threads.py`、`backend/db.py` | 对话 CRUD、`Message`/`ChatQuestion`/`AgentRuntimeSession`/`WorkbenchMeta`/`DraftEvent` |
| 工具面 | `backend/mcp_server.py`(211 行) | ~20 个 FastMCP 工具(coordinator/worker 两种形态) |
| 业务实现 | `backend/draft.py`、`batch.py`、`source_label_inference.py`、`task_control.py`、`worksheet_query.py`、`field_catalog.py`、`vision.py` | 草稿/提案/批处理/反推/范围/字段/视觉 |
| 模型设置 | `backend/model_settings.py`、`capabilities.py`、`build_info.py` | provider 配置、能力开关、版本指纹 |
| 启动 | `scripts/start.ps1`、`scripts/stop.ps1`、`scripts/desktop_bootstrap.py`、`backend/storage_lock.py` | 两种启动路径(源码 / 打包) |

## 3. CLI 进程:启动、关闭、复用

- **启动**:`harness.ACP.__init__` 用 `subprocess.Popen([executable(),'acp','--model',model], cwd=runtime_dir(...), env=environment(...))`。`executable()` 依次找 `REASONIX_EXE` → `<ROOT>/reasonix/reasonix.exe` → PATH。
- **粒度**:`conversation.clients` 是**按数据集**的内存字典 —— **一个数据集同一时刻只有一个 CLI 进程**,不是每个对话一个。
- **复用条件**(`conversation.run`):`clients[did]` 存在且未关闭、进程存活、且 `fingerprint` 相同。fingerprint 含 `model / vision_model / 凭据 / instruction_hash() / reasonix_baseline`。
- **关闭**:以下任一情况 `client.close()`(发 `session/close` → 8s 后 terminate → kill,再关三个流):
  1. 对话切换(`conversation_changed`);
  2. 模型或凭据变化;
  3. `instruction_hash()` 变化 —— **注意:改一行系统提示词,就会让所有在线原生会话失效**;
  4. 进程已退出;
  5. workspace 连接失败后重建一次。
- **native session 复用**:`session_id` 来自 `AgentRuntimeSession.native_session_id`(仅当 status ∈ active/suspended 且 fingerprint 相同);否则 `session/new`。
- **打包侧**:`desktop_bootstrap.py` 取 `exclusive_storage(DATA)` 排他锁后再起 uvicorn;**源码侧 `start.ps1` 直接起 uvicorn,不取锁**(见问题 P4)。

## 4. stdin / stdout / stderr

| 流 | 处理 |
|---|---|
| stdin | `_send()`：`json.dumps(msg)+'\n'`，线程锁内写；进程已退出则抛 `RuntimeError('Reasonix 进程已退出')` |
| stdout | `_read()` 后台线程逐行 `json.loads`；**解析失败静默 `continue`**；`session/update` → 文本/工具/思考事件；`session/request_permission` → 提问或拒绝；`{id}` → 兑现 pending future |
| stderr | `_stderr()` 后台线程，**只保留最后 10 行、每行截 500 字**；`_safe_diagnostics()` 再筛 3 行上报 |

- JSON-RPC 无版本协商、无帧长度、无重连；`rpc()` 用 `future.result(timeout)`，超时统一抛 `RuntimeError('Reasonix <method> 超时')`。
- Reasonix 的**工具返回内容对工作台不可见**:`conversation.run` 的 event 闭包把 `rawInput`/`content` 剥掉。所以模型工具调用的报错只能靠 stderr 间接看到(这正是今天两次误判的成因)。

## 5. Session 如何保存

三层,互相牵动:

1. **DB 绑定**:`AgentRuntimeSession`(dataset_id, conversation_id, native_session_id, model, vision_model, 凭据, fingerprint, status, invalid_reason)。
2. **原生会话文件**:Reasonix 在 `<隔离 home>/sessions/<session_id>.{jsonl,events.jsonl,acp.json,ckpt/}` 自写。隔离 home = `STORAGE/datasets/<did>/reasonix`。
3. **首次恢复时的迁移**(`runtime_isolation.environment`):若隔离 home 无该 session,则去 `provider_home()/sessions/<id>.*` 复制到隔离 home **并**备份到 `STORAGE/backups/session-isolation/<did>/<id>/`,写 `migration.json`(含各文件 sha256),`mark_resumed()` 再把状态置 `acp_resume_verified`。找不到就**硬失败**:`会话隔离迁移阻塞…保留原ID，不新建替代会话`。
4. **网页侧事实**:`Message`(每次回合,含 presentation/events/technical)+ `WorkbenchMeta('chat-turn:<turn_id>')` + `chat_thread`(对话,软删除)。

## 6. 流式输出如何传输

`draft.emit(s,did,kind,payload)` → 插一行 `draft_event` → `GET /api/drafts/{did}/events` 每 0.5s 拉一次 → SSE 帧 → 前端按 `kind` 归并。用户可见文本来自 `chat_chunk` 累加;进度来自 `message_presentation.progress`;工具活动来自 `message.events`。

## 7. Tool Call 如何识别与执行

1. `runtime_dir()` 生成 `reasonix.toml`,其中 `[[plugins]] name="product_workbench"`,`command=sys.executable`,`args=["-m","backend.mcp_server"]`,`auto_start=true`,`tier="eager"`,并注入 `env`。
2. `[permissions] mode="deny"` + 显式 `allow=[...mcp__product_workbench__*]` / `deny=[bash,write,edit,read_file,...,run_skill]`。**不在 allow 里的调用会变成 ACP `session/request_permission`**,`_read` 一律当作“未开放工具”拒绝。
3. 模型经 `use_capability` 代理调用 MCP 工具;工作台只能看到 `tool_call/tool_call_update` 的 `title/status/rawInput`(拿不到参数细节与返回内容)。
4. 工具体在 **MCP 子进程**内执行,直接读写同一个 SQLite 库(draft/batch/task_control/worksheet_query)。
5. 工具语义完全靠 docstring 教给模型 —— 今天 deepseek 那轮 9 次失败全部源于工具契约与文档不一致(见 P8)。

## 8. Working Directory 如何管理

`harness.runtime_dir(dataset_id, suffix, ...)` = `STORAGE/harness/<did>/<suffix>`:
- `suffix = run_id`(worker)、`chat-<conversation_id>`(coordinator 有对话)、`chat`(无对话);
- 目录内写 `PRODUCT_ANALYST_AGENT.md` + `reasonix.toml`;
- 该目录既是 Popen 的 `cwd`,也是 `system_prompt_file` 的基准。

**cwd 按对话变化** → 切对话即换目录,必然重建进程。

## 9. 环境变量与 API Key 如何注入

`runtime_isolation.environment(dataset_id, session_id, credential_ids)`:

- `env = dict(os.environ) + REASONIX_HOME=<隔离home> + TAG_PROVIDER_HOME=<provider_home()>`
- 写 `<隔离home>/config.toml`:**只取** `providers/default_model/model`,不导入全局 plugins/memory/permissions;按所选凭据改写 `api_key_env` 与 `primary_credential_id`
- 写 `<隔离home>/.env`:**只含所选 provider 的密钥**,从 `provider_home()/.env` 读取;Windows 上 `icacls /inheritance:r /grant:r <user>:F`
- 注释明确:Reasonix 1.25.1 **只从它自己 home 的 `.env` 读 provider 密钥**,不认环境变量覆盖
- MCP 子进程另有一套:`PYTHONPATH/TAG_STORAGE/TAG_PROVIDER_HOME/TAG_DATASET_ID/TAG_VISION_MODEL/TAG_MCP_PROTOCOL/TAG_CONVERSATION_ID/TAG_DISABLE_VISUAL_TAGGING/TAG_CACHE`

`provider_home()` 解析顺序:`TAG_PROVIDER_HOME` → `REASONIX_HOME` → `%APPDATA%\reasonix`。**源码运行若不显式设置 `TAG_PROVIDER_HOME`,会读到用户全局 Reasonix 配置**,与安装版授权不一致(今天已实测踩到)。

---

## 10. 最易导致不稳定的节点(按发生概率 × 影响排序)

| # | 节点 | 证据 / 机理 |
|---|---|---|
| P1 | **进程与原生会话的生命周期绑得太死** | `clients[did]` 是内存态、按数据集单例;`AgentRuntimeSession` 是 DB 态;session 文件是磁盘态。三方需同时一致,任一变化都走 `close()+park+重建`。用户观感是“会话没了” |
| P2 | **`instruction_hash` 把系统提示词纳入会话身份** | 提示词一改,所有在线会话失效并重建。今天我自己改提示词就触发了 |
| P3 | **对话切换 = 换 cwd + 关进程** | cwd 含 conversation_id,切对话必然重建;且一个数据集同时只能有一个会话,多对话并行不可能 |
| P4 | **源码启动路径没有存储排他锁** | `exclusive_storage` 只在 `desktop_bootstrap.py` 与 `restore_backup.py` 用到;`start.ps1` 直接起 uvicorn。同一数据目录可被两个进程共享(今天只能靠快照规避) |
| P5 | **session 文件迁移是硬失败点** | `environment()` 要求 `provider_home()/sessions/<id>.acp.json` 存在,否则报“迁移阻塞”;`icacls` 失败也直接抛错。都在我们控制之外 |
| P6 | **stderr 只留 10 行、上报 3 行;stdout 解析失败静默丢弃** | 真实病因(auth 401 / MCP 超时 / 工具 traceback)极易被 MCP 的 INFO 日志挤掉。今天连续两次误判为“会话异常” |
| P7 | **错误归类是文本/正则启发式** | `_classify_error` 靠拼接 detail+stderr 猜 HTTP/超时/权限。一天内为它补了三次补丁 |
| P8 | **工具契约是可靠性的最短板** | ~20 个工具,语义只靠 docstring;`query_scope` 承诺 `scope_id` 而 `propose_changes` 的 targets 形式不认、`raw:` 前缀易误读、`read_repair_scope` 无法列举 —— 今天 deepseek 那轮 27 次调用 9 次失败全在此 |
| P9 | **流式是 DB 轮询** | 0.5s 粒度;异步生成器里做同步 SQLite 查询,连接数一多就压事件循环;`draft_event` 只增不减 |
| P10 | **阻塞点曾无上限,现在靠常量兜底** | `session/prompt` 原为 `timeout=None`(今天才加看门狗);看门狗只在本地 `cancel()`,Reasonix 是否肯在工具执行中途让出,未经验证 |
| P11 | **上下文膨胀** | 系统提示词约 4KB + 规则 core + 模块 ID;生产实测单轮 22355 tokens,弱模型易陷入工具试错循环(54 次调用 / 0 字正文) |
| P12 | **多进程共写一个 SQLite** | 主进程 + MCP 子进程 + 后台批处理线程都写同一库,靠 `draft.WRITE_LOCK`(进程内)与 SQLite 锁(跨进程)。进程内锁对跨进程无效 |

---

## 11. 最小化重构方案:UI → Agent Bridge → Reasonix Runtime

### 目标形态

```
浏览器  frontend/src/agent/AgentPanel.jsx        （唯一会话）
   │  POST /api/agent/message   {text}
   │  GET  /api/agent/events    SSE（只追加事件表）
   │  POST /api/agent/stop
   ▼
backend/app.py                                    （薄路由，只做参数校验）
   ▼
backend/agent/bridge.py                           （唯一入口；不 import 任何业务模块）
   ├─ runtime.py    进程 + JSON-RPC + 单次调用超时 + 中止
   ├─ protocol.py   帧编码/解析（纯函数，可独立单测）
   ├─ session.py    agent_session 表：native_session_id 持久化
   ├─ events.py     agent_event 表 + sink（追加写）
   └─ config.py     reasonix.toml / AGENT.md / env / .env 生成
   ▼
Reasonix CLI 1.25.1
   │  stdio MCP
   ▼
backend/mcp_server.py                             （工具面收窄到 5–6 个）
   ▼
业务层：draft / batch / source_label_inference / task_control / worksheet_query
```

### 依赖方向(硬约束)

- `agent/*` **只依赖** `db`(自己的两张表)与标准库;**禁止** import `draft / batch / conversation / business_questions / source_label_inference`。
- 业务侧**只通过** `bridge.AgentBridge` 的三个方法和一个事件 sink 与 CLI 交互。
- 工具面是**唯一**允许业务代码被 Agent 触达的通道(继续走 MCP,职责不变)。

### 保留 / 移除

**保留(本次范围)**
1. 单会话(一个数据集一个会话,无对话切换)
2. 文本输入
3. 流式输出(正文 + 工具状态)
4. Reasonix Session(native_session_id 持久化 + resume)
5. 工具执行(MCP 通道)
6. 中止任务
7. 错误日志(原始证据不截断)

**暂时移除(先断依赖,再按需回归)**
- 多对话 CRUD / 回收站 / 彻底删除、`_handoff_context` 摘要
- 附件与图片上传、`view_chat_image`、商品绑定
- 业务提问桥接(`ask_business_question` + 排队续跑线程)→ 改为“模型提问写进正文,用户下一条就是回答”
- `conversation_presentation` 的正文/操作拆分与 progress 启发式 → 只保留原始事件
- session 文件迁移 + 备份 + icacls(改为“隔离 home 内自管,找不到就新建会话”)
- provider 凭据隔离(改为:启动时把 provider home 显式传入,不再伪造 home)
- 模型能力预校验探针、多 provider 矩阵

### 目标目录结构

```
backend/agent/
  __init__.py
  bridge.py         AgentBridge：start / stop / status / snapshot
  runtime.py        ReasonixRuntime：Popen、rpc、超时、cancel、close
  protocol.py       encode / decode / 事件归一化（纯函数）
  session.py        agent_session 表读写 + fingerprint
  events.py         agent_event 追加 + 读取（游标）
  config.py         reasonix.toml / AGENT.md / env / .env
backend/agent_bridge.py → 删除（旧路径只留 compatibility shim 一个版本）
frontend/src/agent/
  AgentPanel.jsx    输入 + 正文 + 工具状态 + 中止
  useAgentStream.js SSE 订阅与事件归并
tests/agent/
  test_protocol.py  纯函数：帧解析、事件归一化、错误归类
  test_runtime.py   假进程：启动/超时/中止/close 幂等
  test_bridge.py    事件序列、session 复用与失效、错误日志完整
```

### 迁移步骤(每步独立可验证,始终 204 项测试全绿)

| 步 | 动作 | 验收 |
|---|---|---|
| M1 | 把 `harness.py` 的帧处理抽成 `agent/protocol.py`(纯函数)、进程与 RPC 抽成 `agent/runtime.py`。**只搬不改** | 现有测试全绿;新增 protocol 单测 |
| M2 | 新增 `agent_event` + `agent_session` 两张表与 `agent/events.py`;新端点 `GET /api/agent/events`(游标式)。旧 SSE 保留 | 新旧事件流并行可读 |
| M3 | 落地 `agent/bridge.py`,把 `conversation.run` 的“建/复/关进程 + prompt + 事件回调”换成 `bridge.start/stop`。业务编排(presentation、提案)留在 conversation 层 | 一轮对话端到端可用;`clients/busy` 删除 |
| M4 | 删掉 session 文件迁移分支:隔离 home 内没有就 `session/new`,不再读 `provider_home()/sessions`,不再 icacls;`TAG_PROVIDER_HOME` 由启动参数显式传入 | 全新数据目录一次启动成功;旧会话文件缺失不再硬失败 |
| M5 | 删除多对话运行时逻辑:一个数据集一个会话、一个进程;fingerprint 变化时**丢弃重建并记审计**,不再 park/suspended/handoff 摘要 | 切模型只重建一次,UI 明示“已重建会话” |
| M6 | 删除 `business_questions` 的排队续跑线程:提问写进事件与正文,用户回答=普通新回合 | 提问→回答→继续 无后台线程;`_resume_queued` 移除 |
| M7 | 收窄工具面到 5–6 个:`read_sheet`、`read_tasks`、`query_scope`、`propose_changes`、`ask`、`read_rules`。每个 docstring 用「入参形状 + 一个正确示例 + 常见错误」三段式重写 | 用弱模型跑 3 条典型指令,工具失败为 0 |
| M8 | 前端换 `AgentPanel` + 新事件流;移除进度启发式,只显示真实事件与时间戳 | 首字节 < 5s 可见;中止 5s 内生效 |

**每步都要建立的不变量**

1. 单写入者:`agent_event` 只由 bridge 写;业务结果只由 mcp_server 写。
2. 单实例:任何启动路径(含 `start.ps1`)都必须先取 `exclusive_storage` 排他锁。
3. 所有阻塞点有上限:进程启动、initialize、单次 RPC、单次工具调用,全部带超时并记录实测耗时。
4. 错误必须带原始证据:保留完整 stderr 尾部(不截到 3 行),并在 UI 可展开。
5. 事件表只追加 + 定期归档(避免无限增长)。
6. 提示词不计入会话身份:会话失效只由 model/凭据决定,提示词变化不重建进程。

### 暂不处理(需另行决策)

- 多进程共写 SQLite(P12):本次只收窄写入者,不引入独立服务。
- 流式改真推流(P9):保留游标轮询,把间隔降到 200ms 并加事件表归档。
- 上下文预算与工具循环治理(P11):属于提示词与工具设计,放 M7 之后单独做。

---

## 12. 更小的一种做法(优先于第 11 节的 M1–M8)

第 11 节其实是"目标架构 + 分期迁移",其中 M2(新事件表+新端点)、M7(收窄工具面)、M8(新前端)分别是**并行实现、产品决策、新 UI**,不属于最小重构;M1 新建 `backend/agent/` 包也非必要 —— `harness.py` 已经零业务依赖(只 import stdlib + `db`,业务耦合只在 `conversation.py`)。

**判断依据:`conversation.run` 里整套复用/交接机制(park/suspended、`_handoff_context`、`instruction_hash` 参与指纹、对话切换重建)的唯一目的是复用进程。而 Reasonix 的上下文由 native session(`session/resume`)保住,不需要进程常驻。**

| 步 | 动作 | 净代码 |
|---|---|---|
| N1 | 把 `conversation.run` 的"选/建/复/关进程 + prompt + 事件回调"搬成 `harness.Session` 门面,只暴露 `prompt(text,on_event)` / `cancel()` / `close()` | ≈0(搬移) |
| N2 | 每回合 `close()`;只持久化 `native_session_id`;指纹只含 model + 凭据;**删 `_handoff_context`、删 park/suspended、删 `instruction_hash` 参与指纹** | **约 −100 行** |
| N3 | `start.ps1` 与 app `lifespan` 都取 `exclusive_storage`;`_stderr` 保留完整尾部;stdout 解析失败记一行 | +10 行 |
| N4 | 工具面只改 docstring 与错误信息(形状+示例+常见错误),不删工具 | 行为不变 |

不新增包、不新增表、不新增端点、不动前端。

代价:
- 每回合冷启动数秒(回合本身以分钟计,可接受)。
- **保留** `runtime_isolation` 的 session 文件迁移分支(只在某 home 首次 resume 触发,非不稳定源);若删掉,已有原生会话将无法续接。
- 删 `_handoff_context` 后,提示词变化不再触发会话重建 —— 即 P2 的根治。

**不属于最小重构、需单独决策**:M7 工具面收窄(产品边界)、M2 新事件表+真推流(性能)、M8 新前端(只有"单会话"定案后才值得)。

## 附:本次审计用到的实测证据

- `query_scope(job_id=<反推任务>)` → 裸 `KeyError('expected')`(已修 `8e5c641`)
- provider 401 被报成“会话异常结束”(已修 `8e5c641`)
- tools 事件全部退化成“正在读取工作台信息”(已修 `8e5c641`)
- `scope_id` 无法经 targets 传递 → 一轮 5 次失败(已修 `322e508`)
- 提问回答因默认对话被回收而无法续跑(已修 `57ec211`)
