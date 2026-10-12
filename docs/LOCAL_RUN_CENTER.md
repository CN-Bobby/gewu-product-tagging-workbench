# 格物本地运行中心（M1）

`scripts/install-run-center.ps1` 用于安装当前用户的运行中心配置和三个桌面入口。它只写入 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter` 与桌面快捷方式，不会启动服务。已知的旧 beta.7 正式快捷方式和旧 TEST 快捷方式会移入 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter\LegacyShortcuts` 留存；目标不明的同名快捷方式会让安装停止，不会覆盖。

## 固定入口

| 入口 | 页面/API | 代码 | 数据 | 缓存 |
| --- | --- | --- | --- | --- |
| DEV | `http://127.0.0.1:5174` / `8896` | `config.json` 中的 `projectRoot` 日常开发工作树 | `K:\GeWuDev\Data` | `K:\GeWuDev\Cache` |
| TEST | `http://127.0.0.1:8895` | `K:\GeWuTest\Current\app` | `K:\GeWuTest\Data` | `K:\GeWuTest\Cache` |
| PROD | `http://127.0.0.1:8765` | 通过提升凭证配置的 `K:\Bobbytagging\Candidate-beta.N\app` | `K:\Bobbytagging\Data` | `K:\Bobbytagging\Cache` |

## 本地候选交付顺序

日常开发只在 DEV 工作树进行。冻结脚本要求固定候选工作树指向同一完整提交且干净，然后生成 `builds/<版本>-candidate.<提交前12位>/`：包含程序目录、可直接解压的便携 ZIP 和 `.sha256` 文件。TEST 从该 ZIP 解压后启动，候选报告、程序清单和 ZIP 哈希共同绑定人工验收清单；人工结果通过后，提升脚本将这份 ZIP 解压到新的 beta 目录，只改 Run Center 的 PROD 程序路径，保留原 beta 目录和正式 Data/Cache。

人工验收通过后，接受的提交再进入 GitHub `main`。在线更新读取 `GEWU_UPDATE_REPOSITORY`（默认 `CN-Bobby/gewu-workbench-releases`）的 GitHub Releases 元数据，并提供 Release 中便携 ZIP 的下载链接和 SHA-256；客户端不拉取 `main` 分支文件。发布时上传已人工验收的同一 ZIP 与侧车文件，不重新构建。默认二进制仓库当前尚需建立或配置，仓库不可用时检查更新会明确失败。

DEV 与 TEST 的模型、SellerSprite、Sorftime 凭证存于当前 Windows 用户 DPAPI vault；DEV/TEST 共用凭证 scope。各自的非秘密模型提供方配置放在 `RunCenter\profiles\DEV\providers` 和 `RunCenter\profiles\TEST\providers`，与业务数据分开，因此 TEST 数据重置不会删除模型选择。PROD 继续使用当前候选及现有数据目录的配置和凭证，不做自动迁移。首次设置先从 `K:\Bobbytagging\Data-Test` 预览并导入 Key，再单独复制不含密钥的 `providers\config.toml`：

```powershell
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test --apply
python .\scripts\import_provider_settings.py
python .\scripts\import_provider_settings.py --apply
```

两个导入工具默认只预览；凭证导入不验证 Key、不联网，源文件保留。提供方设置脚本只会新建目标文件，目标已有不同配置时拒绝覆盖。TEST/DEV 之后在页面里保存的新凭证只进入 DPAPI vault，模型和服务选择保存在各环境的 `providerHome`。

`config.json` 的 `candidateProjectRoot` 指向一份固定、干净的 TEST 构建工作树。冻结脚本拒绝从日常 DEV 工作树或主工作树构建候选。两个长期 worktree 足够；只有真正需要并行开发时才增加临时 worktree。

首次安装时可以指定 DEV 源码目录和固定候选构建工作树；TEST Current、PROD beta.9 默认程序目录以及三个环境的数据、缓存和报告目录有固定默认值。重装时保留既有 Run Center 配置。PROD 程序必须位于版本化 beta 目录，启动前校验人工验收结果、候选报告、便携 ZIP 和 build manifest 的哈希。脚本不写入正式数据目录。

DEV 使用 Python 自动重载和 Vite 热更新。当前机器已有非运行中心管理的 Vite 服务占用 5173，因此固定开发入口采用 5174，原进程保持不动。Vite 代理目标固定读取 `GEWU_DEV_API_URL`，默认 `http://127.0.0.1:8896`，只接受回环 HTTP 的 8896 端口；代理请求要求本地 5174 Host，若带 Origin 则必须是 `localhost` 或 `127.0.0.1` 的 5174 Origin。仓库 Vite 配置不再默认代理正式端口 8765。

桌面入口启动 DEV 或 TEST 时默认允许模型、MCP、Amazon 和其他外部调用，不弹出模式选择框。DEV 使用 `GEWU_PROFILE=dev` 和专属 `GEWU_DEV_EXTERNAL_EFFECTS`，数据/缓存为 `K:\GeWuDev\Data` 与 `K:\GeWuDev\Cache`；TEST 使用 `GEWU_PROFILE=test` 和专属 `GEWU_TEST_EXTERNAL_EFFECTS`，数据/缓存为 `K:\GeWuTest\Data` 与 `K:\GeWuTest\Cache`。外部调用可能产生费用，Run Center 不计量或强制请求/费用上限。需要离线候选冻结或维护时，可显式传入 `-ExternalEffectsMode offline`；默认实例 ID 分别为 `DEV` 和 `TEST`，离线检查分别报告 `offline`。DEV/TEST 不会因为重启而恢复以前排队的任务。

Run Center 启动后读取 `/api/runtime/status`，核对 `profile`、`external_effects.policy` 和实例 ID。若端口已有 listener 但运行策略或身份与此次启动参数不符，启动器会报错并保留现有进程；它不会停止或替换该实例。显式切换离线模式须先停止身份匹配的 DEV/TEST，再以 `-ExternalEffectsMode offline` 启动。DEV/TEST 数据与 PROD 路径重叠、服务端口被未知进程占用、构建清单缺失或运行身份不符时，同样拒绝启动。停止操作仅读取运行中心登记的 PID，并核验可执行文件与命令行身份；身份不符时不会结束进程。

运行中心配置和 PID 记录位于当前 Windows 用户的 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter`。启动入口持有同一独占维护锁；TEST 重置、冻结或回退期间，新启动会安全失败，不能与目录切换并发。停止只验证登记身份，不受维护锁阻挡。可手动查询或停止某个已登记入口：

```powershell
powershell -NoProfile -File "$env:LOCALAPPDATA\GeWuWorkbench\RunCenter\run-center.ps1" -Profile DEV -Action Status
powershell -NoProfile -File "$env:LOCALAPPDATA\GeWuWorkbench\RunCenter\run-center.ps1" -Profile DEV -Action Stop
```

快捷方式安装和服务启动是两个单独动作。安装之后首次打开时，DEV/TEST 数据、缓存和 providerHome 会按配置创建；PROD 仅在确认候选程序清单和现有服务身份匹配后可接入。任何端口冲突都不会自动杀进程。

## 正式数据快照与历史凭证盘点

正式数据快照只读 `K:\Bobbytagging\Data`，逐项核验常规文件 SHA-256，并使用 SQLite online backup 建立一致的数据库副本。目标必须是新的、位于 `K:\Bobbytagging\Backups` 下的目录；脚本排除凭证文件、日志、SQLite WAL/SHM 和运行锁，拒绝覆盖已存在的目标。已核验的快照 `K:\Bobbytagging\Backups\pre-run-center-20261008` 留作回退证据，源数据未被改动。

```powershell
$target = 'K:\Bobbytagging\Backups\pre-run-center-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
python .\scripts\backup-production-data.py --target $target
python .\scripts\backup-production-data.py --target $target --apply
python .\scripts\inventory-legacy-credentials.py --output 'K:\GeWuTest\Reports\legacy-credential-inventory.json'
```

凭证盘点报告只记录文件路径、类型和元数据，不包含密钥值；盘点不删除旧数据目录或历史备份中的凭证文件。DPAPI 库状态检查只输出每类凭证“已配置/未配置”。
