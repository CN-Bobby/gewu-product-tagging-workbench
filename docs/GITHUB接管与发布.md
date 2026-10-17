# GitHub 接管与发布

> 当前执行规则以 [`AGENTS.md`](../AGENTS.md) 与 [`RELEASE_GOVERNANCE.md`](RELEASE_GOVERNANCE.md) 为准。本页中的首次接管命令和 beta.3 信息是历史记录；仓库目前为公开仓库，当前发布必须遵守“DEV 人工验收 → 冻结 → PROD 核验 → GitHub Release”流程。

## 仓库边界

源码仓库目前为公开 GitHub 仓库。源码、Skill、测试、构建脚本、锁定依赖、版本记录和脱敏过程证据进入 Git；`storage/`、`releases/`、客户表格、图片、SQLite、缓存、Reasonix 会话和凭据不进入 Git。

保留本地已有提交历史和三个开发分支：`baseline/takeover`、`fix/0.4.1-closeout`、`feature/0.5.0-delivery`。外部验收确认后已创建默认分支 `main`，并将 `main` 与交付分支推进到 commit `14603bff0ad9678699817d22d793437bcbdf7398`。

## 首次接管

GitHub 上创建空的私有仓库，不勾选自动 README、License 或 `.gitignore`，避免产生无关初始提交。登录使用 `gh auth login` 或 Windows 凭据管理器，不在命令行或聊天中写入 Token。

```powershell
gh repo create <owner>/<repo> --private --source . --remote origin --push=false
git push origin baseline/takeover fix/0.4.1-closeout feature/0.5.0-delivery
```

推送前运行 `python scripts/repository_audit.py`、`python scripts/sync_version.py --check` 和完整测试。只推送明确的 `heads/*`，不推送 Codex 临时 refs 或未引用对象。

## 历史 Release 记录

早期版本从 commit `14603bff0ad9678699817d22d793437bcbdf7398` 创建不可移动标签 `v0.5.0-beta.3`，并发布 GitHub Pre-release：https://github.com/CN-Bobby/gewu-product-tagging-workbench/releases/tag/v0.5.0-beta.3。该记录仅描述 beta.3 的历史状态，不代表当前版本。每次新发布的先决条件、PROD 身份核验、tag/commit 对齐和绿色包附件要求见 `RELEASE_GOVERNANCE.md`。

Release 说明必须引用应用版本、commit、构建时间、Skill SHA256、Reasonix 版本与 SHA256、schema 版本、安装/升级/回退步骤和已知限制。`SHA256SUMS.txt` 校验全部附件，分发 ZIP 另附 `.sha256` 文件。

## 本机丢失后的恢复

从 GitHub 克隆后按 `docs/WINDOWS_BUILD.md` 安装开发环境，执行 `scripts/fetch_build_inputs.py` 下载并校验锁定的 Python、NSIS、Reasonix 和 wheel，再运行测试和候选构建。客户业务现场另从独立备份恢复 SQLite、原表、图片、缓存和 Reasonix 状态。
