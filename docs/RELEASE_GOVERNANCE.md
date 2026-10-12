# 版本与发布规则

应用版本唯一来源为根目录 VERSION.json。后端直接读取，Vite构建注入界面；npm元数据通过 scripts/sync_version.py 生成，CI使用 --check 拒绝漂移。RELEASE_LEDGER.json记录不可复用的客户版本序列和替代关系。

本地交付顺序为 DEV 修改 → 固定干净提交 → `--portable` 生成 ZIP/侧车 → 从该 ZIP 激活 TEST → 人工验收并绑定 ZIP、app manifest、候选报告哈希 → 提升同一 ZIP 到新 beta 程序目录 → PROD 校验提升凭证。候选进入源码仓库 `main` 并完成 PROD 提升后，`scripts/publish-portable-release.ps1` 校验人工结果、候选报告、提升凭证与资产哈希，再将完全相同的人工验收 ZIP/侧车上传到二进制 GitHub Releases；发布说明记录源码 `main` 的完整 commit，二进制仓库的 Release tag 使用产品版本号。在线更新只读 Release 元数据和 assets，不从分支同步源码。源码仓库遗留的 beta tag 工作流会拒绝重构 beta ZIP，避免替换已验收附件。

日常开发提交不更新客户版本号；内部开发构建以版本号和Git commit共同标识。准备冻结客户试用/验收候选时，才递增到未使用过的`beta.N`，并同步VERSION.json、RELEASE_LEDGER.json、CHANGELOG.md及派生版本元数据。候选对外发布、交付或冻结后不得覆盖；改变后的替代程序使用新的beta号。

接管基线位于 baseline/takeover，只记录已有代码，不代表已验收发行版。当前开发分支为`feature/0.5.0-delivery`。main仅在里程碑验收通过时创建/推进。发布标签不可移动，安装包不可覆盖。Git tag只固定源码提交，不代表GitHub Release、部署或验收；每项状态必须由对应证据支持。历史源码标签可以追溯补建，但须明确标记为历史源码引用，不能改变原验收结论。

里程碑：0.4.1现有功能收尾 → 0.5.0-alpha.1项目模板、严格复用、独立状态 → 0.5.0-beta.1首次外测 → 0.5.0-beta.2交互安装缺陷版 → 0.5.0-beta.3简化安装候选。只有通过对应验收才能变更release_status，不能靠改版本号宣称完成。

所有任务冻结application_build：应用版本、commit、dirty和源码摘要。Skill独立版本及内容摘要、Reasonix二进制版本及摘要、数据库递增迁移编号分别进入发布清单。商品V1/V2不是软件版本。

正式发布必须来自干净Git树，精确锁定依赖，测试报告绑定commit。内部候选文件名使用`<版本>-candidate.<短commit>`；客户提升文件只含产品名和版本号。客户附件一旦提升即冻结，任何变化递增`beta.N`，禁止使用“修复版、最新版、最终版”或覆盖同版本。构建附件存入releases/<version>，不进入源码Git；已有目录拒绝覆盖。发布清单记录构建时间、操作系统、Python、Skill和依赖版本。测试数据、SQLite、会话、密钥和客户原图禁止打包。

升级前同时考虑数据库与Reasonix会话状态备份。新版在隔离副本验证恢复旧会话ID；失败列为阻塞，禁止静默创建新会话。安装器不得覆盖客户数据；二进制回退必须检查状态格式兼容性。

状态只用：未完成、已实现待验收、已验收、阻塞。进度报告必须写当前commit、实际运行版本、已验收、待办、阻塞和产物路径。历史验收报告保留原日期，不作为新版本验收凭证。
