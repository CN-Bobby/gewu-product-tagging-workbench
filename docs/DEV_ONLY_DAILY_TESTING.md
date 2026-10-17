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
- `freeze-test-candidate.ps1`、TEST 激活/回退、TEST 人工清单等属于历史候选链路，不是当前交付流程；不得要求新版本先激活 TEST。当前候选由 DEV 人工验收通过后冻结，再按发布规则提升到 PROD。

## 分支与发布

- 各并行功能分支在准备验收时汇总到当前 DEV 工作树，再在 DEV 做统一人工测试；合入或运行在 DEV 本身不代表已冻结、已部署 PROD 或已发布 GitHub。
- 日常开发构建使用版本号加 Git commit 标识，不递增客户 beta 版本。
- 用户确认 DEV 人工测试通过后，冻结唯一候选版本并绑定源码提交、构建身份、测试记录和绿色版包哈希；冻结后不覆盖，任何修改递增新版本。
- 冻结后把同一提交和产物提升到 PROD，核验生产实例的版本、build ID、commit 后，默认发布同版本 GitHub Release，并附绿色便携包与 SHA256。若另一台电脑安装验收是该版本的明确门禁，则保持待验收状态并单独记录。
- 在线更新从公开源码仓库 `CN-Bobby/gewu-product-tagging-workbench` 的 GitHub Releases 读取已发布 ZIP 和 SHA-256，不同步源码分支。Release 必须对应已核验的 PROD 版本；DEV/TEST 环境变更、普通候选构建均不等同于正式发布。
