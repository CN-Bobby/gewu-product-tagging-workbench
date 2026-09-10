# GitHub 接管与发布

## 仓库边界

使用一个私有 GitHub 仓库。源码、Skill、测试、构建脚本、锁定依赖、版本记录和脱敏过程证据进入 Git；`storage/`、`releases/`、客户表格、图片、SQLite、缓存、Reasonix 会话和凭据不进入 Git。

保留本地已有提交历史和三个开发分支：`baseline/takeover`、`fix/0.4.1-closeout`、`feature/0.5.0-delivery`。外部干净 Windows 验收通过前，不创建 `main` 或 `v0.5.0-beta.1`。

## 首次接管

GitHub 上创建空的私有仓库，不勾选自动 README、License 或 `.gitignore`，避免产生无关初始提交。登录使用 `gh auth login` 或 Windows 凭据管理器，不在命令行或聊天中写入 Token。

```powershell
gh repo create <owner>/<repo> --private --source . --remote origin --push=false
git push origin baseline/takeover fix/0.4.1-closeout feature/0.5.0-delivery
```

推送前运行 `python scripts/repository_audit.py`、`python scripts/sync_version.py --check` 和完整测试。只推送明确的 `heads/*`，不推送 Codex 临时 refs 或未引用对象。

## Release

验收通过后，从干净工作区创建不可移动标签 `v0.5.0-beta.1`，发布 GitHub Pre-release。安装程序、客户分发 ZIP、源代码 ZIP、验收证据、发布清单、SHA256、许可证和说明作为 Release 附件，不提交到 Git 历史。

Release 说明必须引用应用版本、commit、构建时间、Skill SHA256、Reasonix 版本与 SHA256、schema 版本、安装/升级/回退步骤和已知限制。`SHA256SUMS.txt` 校验全部附件，分发 ZIP 另附 `.sha256` 文件。

## 本机丢失后的恢复

从 GitHub 克隆后按 `docs/WINDOWS_BUILD.md` 安装开发环境，执行 `scripts/fetch_build_inputs.py` 下载并校验锁定的 Python、NSIS、Reasonix 和 wheel，再运行测试和候选构建。客户业务现场另从独立备份恢复 SQLite、原表、图片、缓存和 Reasonix 状态。
