# DEV 唯一日常人工测试环境

生效日期：2026-10-13

## 日常开发与人工测试

- 新功能合入当前 DEV 工作树后，直接在 DEV 做人工测试；不再先部署到独立 TEST。
- DEV 页面/API 为 `http://127.0.0.1:5174` / `http://127.0.0.1:8896`，业务数据与缓存位于 `K:\GeWuDev\Data` 和 `K:\GeWuDev\Cache`。
- DEV 当前使用 `offline` 策略。需要访问模型、MCP、Amazon 等外部服务时，按具体任务显式切换并确认费用与调用范围；人工测试结果要与自动化测试、真实外部调用结果分别记录。
- PROD `8765` 仍是正式环境，不用于开发或人工测试。

## TEST 退役与保留

- 独立 TEST 服务 `8895` 已停止，不再作为新功能日常验收入口。
- 保留 `K:\GeWuTest\Current`、`Data`、`Cache`、`Reports`、Run Center 配置和 TEST 凭证配置，供审计或回退；不删除、不迁移、不覆盖其中的历史数据。
- `freeze-test-candidate.ps1`、TEST 激活/回退、TEST 人工清单等流程仍是旧候选链路。它们没有因为停止 TEST 服务而自动改成 DEV 流程。后续正式便携候选发布前，须将候选验收门禁明确迁移到 DEV 或确定新的发布验收入口。

## 分支与发布

- 功能分支先合并到当前 DEV 工作树，再在 DEV 做人工测试；本地 DEV 合并不代表已推送 GitHub。
- 日常开发构建使用版本号加 Git commit 标识，不递增客户 beta 版本。
- 在线更新从公开源码仓库 `CN-Bobby/gewu-product-tagging-workbench` 的 GitHub Releases 读取已发布 ZIP 和 SHA-256，不同步源码分支。
- DEV 人工测试通过后，候选从同一个干净提交生成，并以 Pre-release 供另一台电脑安装验收；安装验收通过后再考虑 PROD 部署。DEV/TEST 环境合并本身不等于候选验收或 PROD 发布。
