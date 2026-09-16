# 0.5.0-beta.7 候选验收记录

状态：`pending_acceptance`。候选已构建并通过包内身份、全新安装与 beta.6 数据升级冒烟；真实语义确认与生产切换仍待用户执行。本记录只写已经真实执行并留有证据的项。

候选绑定 release-prep 提交 `82379d2cd6a97229a1dbe48d94d581cee5ddd565`（version `0.5.0-beta.7`，schema 8）。

## 自动化验收

- `82379d2`：`.venv\Scripts\python.exe -m pytest -q` → **596 passed**（本轮仅改发布元数据与文档，业务/Runtime 代码与 `bb0973e` 一致）。
- `82379d2`：`scripts/repository_audit.py` → `forbidden_paths` / `secret_hits` / `sample_hits` 均为空（259 个受控文件）。
- `bb0973e`：`npm run build --prefix frontend` 通过（1583 modules）；`node --test` 前端 **43 passed**。前端源码自该提交后未改动，候选构建过程会重新执行一次前端生产构建。
- `82379d2`：`git diff --check` 通过。

## 候选包与本机升级

- 构建提交：`82379d2cd6a97229a1dbe48d94d581cee5ddd565`（clean worktree，本地锁定输入，全程离线）。
- 安装程序：`releases/0.5.0-beta.7/82379d2cd6a9/格物工作台-0.5.0-beta.7-candidate.82379d2cd6a9-安装程序.exe`。
- 安装程序 SHA256：`4d7c30f30e16c9a9063544d12360f25a041a5c7947fb5b882433bd78f7cb24ed`。
- `source.zip` SHA256：`f7d9e5df1c5623ccf3d2d6cdae38d7a581be32333ac803683739ecb43212daec`。
- `release-manifest.json` SHA256：`7d71f74e2f70a317966e6834caccecbd97fee5afe687c16d2dc27add7f84f827`。
- 包内 `build-manifest.json`：`build_id = 0.5.0-beta.7+82379d2cd6a9`、`database_migration = 8`、2625 个受控文件。

## 真实模型验收（隔离实例，Phase A / Phase B）

在**隔离的 Reasonix 环境**（独立数据目录、独立 provider home、独立端口）上完成，未触碰正式数据：

|范围|结果|
|---|---|
|Phase A（UI Agent 最小真实集）|创建会话、连续两轮上下文（Turn 2 正确复述 Turn 1 的口令）、1 次只读工具调用（`read_worksheet`）、显式取消后同一原生会话 resume 成功（Runtime 换新 pid、native session 不变）、迟到输出未串入下一轮；一次真实原生提问（3 个 ask 在同一回合内被回答后回合正常结束）|
|Phase B（功能补齐）|重启后数据/历史/Agent 事件可读、启动恢复把非终态行置为安全状态、新配置快照对新会话生效、最小真实草稿执行（1 文本列 + 1 视觉列，job `completed`，真实 vision 调用写入观察快照）、批处理 active 期间模型设置变更被拒（409）且配置未变|
|额度计量|文本 prompt 3 次、vision 1 次、批次 1 个 job/1 商品、无额外工具与视觉；未为了制造等待而扩大任务|

## 候选交付验证（bb0973e 候选，代码与本候选相同）

- **全新安装**：隔离目录安装成功，`/api/health` 的 version/build_id/commit 与包内清单一致，前端可加载，持续草稿与 Agent 面板在真实浏览器中渲染正常，Settings 页面可读，正常停止后无该实例遗留的 `reasonix` 进程。
- **升级**：在**数据副本**上从 beta.6 升级到新候选；自动生成升级前备份，schema 保持 8 并补齐新表，dataset/草稿/列/行/历史对话/消息/任务全部可读，1 个数据集 310 件商品与 **17 个已填值**保留；另有独立副本验证了 **schema 7 → 8 迁移**（beta.5 数据）。
- **回退**：用**旧候选程序 + 升级前备份恢复**完成（不是原地降级）；回退后旧程序可启动、310 行与 17 个已填值仍可读。

## beta.7 packaging identity 冒烟

- 全新安装：安装器 exit 0，关键文件齐备，`/api/health` = `0.5.0-beta.7+82379d2cd6a9`（`dirty=false`），前端 200 且真实浏览器头部渲染 `0.5.0-beta.7`，Settings 页面可读，正常停止、无遗留进程、无 Traceback。
- beta.6 数据副本升级：健康检查为 `0.5.0-beta.7+82379d2cd6a9`，310 件商品 / 310 单元格 / **17 个已填值** / 10 条消息 / 3 个对话 / 3 个任务保留，`settings_configured=true`，自动生成 `backups/upgrade-*`（含数据库），无 Traceback。

## 已知限制（不阻塞交付，本轮不修复）

- 全新安装未保存模型服务与授权前，无法启动 Agent 会话（面板会先显示模型设置）。
- 删除凭据后，该数据集的运行时目录可能残留旧密钥直到下一次启动重写。
- 跨进程重启不保留凭据身份（重启后旧会话不在进程内，按历史记录只读展示）。
- Agent 面板未指定模型时使用内置默认模型，与设置项里的默认模型不一致。

## 发布门槛

源码自动化、仓库审计、真实浏览器加载、真实模型最小验收、候选全新安装/升级/回退均已通过。**真实数据上的语义确认与生产环境切换仍待用户执行**；在此之前不创建 tag/Release，不将源码 `release_status` 改为 `accepted`。
