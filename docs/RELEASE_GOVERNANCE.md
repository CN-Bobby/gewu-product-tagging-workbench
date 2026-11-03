# 已退役：本地候选和 DEV / TEST / PROD 发布门禁

退役日期：2026-11-03

本文件此前描述本地候选冻结、多环境验收和逐级提升流程。这些流程不再用于日常开发。

当前规则：

- 普通修复在 `D:\数据打标项目` 的当前分支完成；用户要求提交或推送时，处理当前分支上的本次改动。
- 普通修复不改应用版本号，不创建本地候选包，也不要求 DEV/TEST/PROD 提升。
- 只有用户明确要求打包或发布时，才按指定源码提交创建 GitHub Release。`VERSION.json`、`RELEASE_LEDGER.json` 和 `CHANGELOG.md` 继续作为应用和历史 Release 记录保留，不构成日常工作门禁。
- 旧 Release、tag、构建产物和验收报告仍是历史记录，不因流程退役而改写或删除。

工作区规则见 [`LOCAL_WORKSPACE_MODEL.md`](LOCAL_WORKSPACE_MODEL.md)。数据、凭据和运行配置按用户确认单独处理。
