# 项目协作与版本规则

每个新任务开始前，先检查 `git status`、当前分支、HEAD 和上游状态，不得覆盖或清理用户已有的工作区改动。先阅读：

1. `PROJECT_HANDOFF.md`
2. `docs/RELEASE_GOVERNANCE.md`
3. `VERSION.json` 与 `RELEASE_LEDGER.json`
4. `CHANGELOG.md`
5. 与当前任务相关的交接、验收和交付文档

以当前仓库文件和实时 Git 状态为准；旧对话、记忆、版本号或标签不能代替当前状态核验。若项目文档冲突，先指出冲突，并按 `docs/RELEASE_GOVERNANCE.md` 的版本规则处理。

## 软件版本更新时机

- 普通开发提交和日常绿色测试构建不递增客户版本号。开发构建用 `VERSION.json` 的版本加 Git commit 标识，例如 `0.5.0-beta.7+<commit>`，并通过构建清单标出 dirty 状态。
- 冻结一个准备给客户试用或验收的候选版本时，才选择下一个从未使用过的 `beta.N`，并同步更新 `VERSION.json`、`RELEASE_LEDGER.json`、`CHANGELOG.md` 和派生版本元数据。
- 候选一旦对外发布、交付或作为客户候选冻结，内容不可原位替换。若要用改变后的程序替代它，必须递增到新的 `beta.N`。
- 版本号、Git tag、GitHub Release、部署状态和验收状态是不同事实。Tag 只固定源码提交；只有相应验收和发布证据齐备，台账状态才能标为已验收或已发布。
- Tag 必须指向实际构建源码提交且不可移动。历史源码标记可以补建，但必须注明是追溯源码标记，不得据此声称当年验收门禁通过或创建了 GitHub Release。

常规任务默认只提交到本地，不推送 GitHub；仅在用户明确要求同步/推送时推送。不得为版本整理永久删除历史文件或覆盖正式数据。
