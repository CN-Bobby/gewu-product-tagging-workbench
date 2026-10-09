# 格物本地验收流程

## 固定入口与隔离

| 环境 | 入口 | 端口 | 业务数据 | 外部任务 |
| --- | --- | --- | --- | --- |
| DEV | 桌面“格物 · 打开开发版” | 页面 5174，API 8896 | `K:\GeWuDev\Data` | 每次启动选择 `offline`（默认）或单进程 `live_validation` |
| TEST | 桌面“格物 · 打开验收版” | 8895 | `K:\GeWuTest\Data` | 每次启动选择 `offline`（默认）或单进程 `live_validation` |
| PROD | 桌面“格物 · 正式工作台” | 8765 | `K:\Bobbytagging\Data` | 按正式环境既有权限 |

每个快捷方式由 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter\run-center.ps1` 管理。端口冲突、数据目录重叠、TEST 清单与代码不一致、进程身份或外部调用策略不符时会停止本次启动并保留现有进程，不会停止或替换不匹配的 listener。开发版热更新只在 DEV 使用；TEST 只指向 `K:\GeWuTest\Current\app` 的冻结清单。入口启动与 TEST 维护使用同一独占锁，重置、冻结和回退期间不会有新实例并发打开数据或程序目录。

DEV/TEST 在当前 Windows 用户的 DPAPI 凭证库中共享测试凭证，PROD 使用独立 scope。模型提供方配置在 Run Center 下按 DEV/TEST 分开保存，避免 TEST 重置业务数据时丢失模型选择。首次迁移只从 `K:\Bobbytagging\Data-Test` 导入模型与 SellerSprite 凭证；来源文件保留且不验证 Key。PROD 不在本轮迁移。业务数据库、缓存、构建目录仍然分离。版本面板只显示凭证是否配置，不返回值。

## 离线自动回归

从 Run Center 配置中的固定候选构建工作树运行：

```powershell
powershell -NoProfile -File .\scripts\run-offline-checks.ps1
```

脚本设置 `GEWU_PROFILE=test`，清除继承的 DEV/TEST live-validation 标记，运行后端 pytest、前端测试和生产前端构建，不会启动工作台、模型、MCP、Amazon 采集或付费任务。pytest 为兼容既有假服务测试，隔离每个用例的 DPAPI 与 Reasonix 路径，并只允许回环网络；外网请求会在套接字层失败。`test_runtime_governance.py` 验证 DEV/TEST 的 profile 专属开关、默认离线、路由门禁与启动恢复不执行。JSON 报告保存在 `K:\GeWuTest\Reports`，包含 commit、构建 ID、脏状态、回环网络保护与各步骤状态，不保存原始密钥或终端输出。自动回归始终保持 offline，即使之后人工启动了 live-validation DEV 或 TEST。

## 固定样本

`K:\GeWuFixtures\project-library-v1` 是本地验收夹具，包含脱敏后的泳衣选品档案和狗狗发声玩具项目，覆盖原始 Excel、Data X、评论 Excel 与 VOC HTML 报告。目录外的 `fixture-manifest.json` 为 `seed-data` 每个文件记录 SHA-256。泳衣 Excel 保留嵌入图片供缩略图验收。夹具不含数据库凭证文件、日志或外部任务。

重置只替换 TEST 数据：

```powershell
powershell -NoProfile -File .\scripts\reset-test-data.ps1
# 查看目标、夹具哈希与当前 TEST 端口状态后，显式执行：
powershell -NoProfile -File .\scripts\reset-test-data.ps1 -Apply
```

执行前会停止登记且身份匹配的 TEST 进程；若 8895 仍有任何 listener，就拒绝重置。原 `K:\GeWuTest\Data` 目录以原样移动到 `K:\GeWuTest\Backups\Data-before-reset-*` 保留，夹具先复制到旁路 staging 并逐文件校验，再切换到固定数据路径。报告写到 `K:\GeWuTest\Reports`，重置不会碰 `K:\Bobbytagging\Data` 或其缓存。

TEST 的 `Data`、`Cache`、`Current\app` 与 `Reports` 路径由脚本固定核验；不能通过参数或修改 Run Center 配置把报告写入 PROD、或把 TEST 回退/重置目标指向正式数据。回退遇到中途错误时会尝试恢复原来的 TEST 程序、Data 和 Cache；失败程序与临时目录会保留在 `Rollback\Failed` 供恢复检查。

## 冻结一个 TEST 候选

只有固定验收工作树位于指定的完整 Git commit 且工作区干净时才可冻结：

```powershell
$commit = git rev-parse HEAD
powershell -NoProfile -File .\scripts\freeze-test-candidate.ps1 -Commit $commit
```

脚本先跑离线回归，再使用 `build_windows.py --dev` 生成无安装器的干净构建，逐项验证 `build-manifest.json` 的文件哈希。候选构建和 `-StartAfter` 都显式选择 offline，不会等待模式对话框或启用真实调用。普通日常修改不更新客户版本号。只有审阅自动报告后，才加 `-Activate` 更新 TEST；程序先进入 staging，数据与缓存建立排除凭证和日志的备份，原程序保留在 `Rollback\Previous`。`-StartAfter` 可在激活后做一次健康与离线身份检查。

更新候选工作树前先确认 `git status --short` 为空，再将它以 detached HEAD 切到已选定的完整提交。冻结脚本会核对它与 `candidateProjectRoot` 完全一致，不接受 DEV 或主工作树作为替代来源。

冻结报告记录候选构建目录、构建清单哈希和离线报告哈希。对待激活报告运行 `-Activate` 时，脚本复用该候选目录与同一份离线报告并重新核对所有身份；不会重跑生成新时间戳的构建，也不会覆盖已激活或身份不同的报告。

如果启动检查失败，先保留报告、Current、Rollback、Backups 和 TEST 数据。只对仍被支持的前一 TEST 构建执行回退；不要将旧 beta.7 当成已通过新离线门禁的版本。

## 人工验收

TEST 候选启动后，桌面入口会每次询问外部调用模式：按 **否**（默认）使用 offline；按 **是**允许本次单进程 `live_validation` 集成调用；按 **取消**或关闭窗口则不启动。live validation 会启用模型、MCP 或 Amazon 等外部调用，可能产生费用。业务数据仍只使用隔离 `K:\GeWuTest\Data`，缓存使用 `K:\GeWuTest\Cache`。环境变量仅传给本次 TEST 进程，运行身份必须显示 `TEST-LIVE-VALIDATION/live_validation`；offline 身份为 `TEST/offline`。该模式开关不计量或强制请求/费用上限；用户须在功能中只提交明确授权的小范围，并在清单填写授权上限和结果。若 8895 已有实例但模式与这次选择不一致，Run Center 保留该进程并拒绝启动，不能自动切换或替换。

创建独立人工清单：

```powershell
powershell -NoProfile -File .\scripts\new-manual-acceptance.ps1
```

在 `K:\GeWuTest\Reports` 生成的 Markdown 清单中逐项操作、将通过项改为 `[x]`，并在有问题时记录页面位置和复现步骤。它绑定当前 TEST policy/build/commit、夹具清单哈希和同 commit 的离线自动回归报告哈希。offline 清单记录 0 次外部调用预期。live-validation 清单必须由用户填写清楚的 live-call 记录：服务、workflow/run ID、选定输入范围、授权额度、用户观察到的调用情况和结果路径；Run Center 不会测量或推断调用数。没有执行外部调用时，明确填写“未执行”和相应结果路径。自动回归报告仍必须是通过且 0 外部请求的 offline 报告。确认每项都完成后归档：

```powershell
powershell -NoProfile -File .\scripts\finalize-manual-acceptance.ps1 -Checklist 'K:\GeWuTest\Reports\manual-acceptance-....md' -Status passed
```

候选激活并确认 TEST 离线身份后，运行固定项目文件树浏览器回归。它只访问 `127.0.0.1:8895`，检查当前页面在展开项目树后不跳转、四类夹具文件、泳衣 Excel 图片、Data X、评论和 VOC 预览、浏览器前进后退及 375px 窄屏；报告和截图都写入 Reports：

```powershell
.\.venv\Scripts\python.exe .\scripts\test-project-library-browser.py
```

以下功能属于本项目的长期验收回归：

- 通过 TEST 桌面入口启动；每次选择 offline 或 live validation，并核对运行状态面板的 policy、instance ID、build、commit 和隔离数据目录。
- 展开和收起全局项目树、进入项目资料、浏览器后退/前进后仍停留在正确页面与项目。
- 泳衣源 Excel 的工作表切换、内嵌图片缩略图与图片预览。
- 狗狗玩具 Data X V1、泳衣和狗狗玩具评论 Excel、VOC HTML 预览。
- 约 375px 窄屏下树、页面导航和预览不遮挡关键操作。
- DEV/TEST 默认 offline；每次启动可分别选择 live-validation，以真实 API/MCP 完成明确发起的功能流程。运行中心不执行调用额度上限；TEST 人工验收清单记录范围与用户观察结果。两种模式下 DEV/TEST 的自动任务恢复都关闭，本地预览仍可用。
- 重启 TEST 后身份与凭证状态持续；所有报告只记录“已配置/未配置”。
- 发生 TEST 数据重置时，正式数据哨兵和哈希不变。

## 凭证首次导入

首次导入只读取既有 `K:\Bobbytagging\Data-Test`，默认先预览；确认报告中的来源与发现数量后加 `--apply`。模型与 SellerSprite 凭证进入 DEV/TEST 共享 DPAPI scope，源文件保留用于人工核对；Sorftime 凭证按方案以后通过设置页重新录入。导入过程不验证密钥、不发起网络请求。

```powershell
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test --apply
python .\scripts\import_provider_settings.py
python .\scripts\import_provider_settings.py --apply
```

凭证库导入是共享 DEV/TEST scope，所以同一次导入可供两个环境使用。提供方设置复制到两个 profile 的独立目录；预览脚本会检查配置未内嵌 Key，已有不同配置时拒绝覆盖。已存在的源凭证文件和旧历史备份不自动删除；需另行列出并人工审阅。`inventory-legacy-credentials.py` 可按路径和类型盘点历史凭证，不会记录值或删除文件。
