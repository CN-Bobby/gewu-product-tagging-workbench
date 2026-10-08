# 格物本地运行中心（M1）

`scripts/install-run-center.ps1` 用于安装当前用户的运行中心配置和三个桌面入口。它只写入 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter` 与桌面快捷方式，不会启动服务。已知的旧 beta.7 正式快捷方式和旧 TEST 快捷方式会移入 `%LOCALAPPDATA%\GeWuWorkbench\RunCenter\LegacyShortcuts` 留存；目标不明的同名快捷方式会让安装停止，不会覆盖。

## 固定入口

| 入口 | 页面/API | 代码 | 数据 | 缓存 |
| --- | --- | --- | --- | --- |
| DEV | `http://127.0.0.1:5174` / `8896` | `config.json` 中的 `projectRoot` 日常开发工作树 | `K:\GeWuDev\Data` | `K:\GeWuDev\Cache` |
| TEST | `http://127.0.0.1:8895` | `K:\GeWuTest\Current\app` | `K:\GeWuTest\Data` | `K:\GeWuTest\Cache` |
| PROD | `http://127.0.0.1:8765` | `K:\Bobbytagging\Candidate-beta.8\app` | `K:\Bobbytagging\Data` | `K:\Bobbytagging\Cache` |

DEV 与 TEST 的模型、SellerSprite、Sorftime 凭证存于当前 Windows 用户 DPAPI vault；DEV/TEST 共用凭证 scope。各自的非秘密模型提供方配置放在 `RunCenter\profiles\DEV\providers` 和 `RunCenter\profiles\TEST\providers`，与业务数据分开，因此 TEST 数据重置不会删除模型选择。PROD 继续使用当前候选及现有数据目录的配置和凭证，不做自动迁移。首次设置先从 `K:\Bobbytagging\Data-Test` 预览并导入 Key，再单独复制不含密钥的 `providers\config.toml`：

```powershell
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test
python .\scripts\import_credentials_to_vault.py --source-storage 'K:\Bobbytagging\Data-Test' --profile test --apply
python .\scripts\import_provider_settings.py
python .\scripts\import_provider_settings.py --apply
```

两个导入工具默认只预览；凭证导入不验证 Key、不联网，源文件保留。提供方设置脚本只会新建目标文件，目标已有不同配置时拒绝覆盖。TEST/DEV 之后在页面里保存的新凭证只进入 DPAPI vault，模型和服务选择保存在各环境的 `providerHome`。

`config.json` 的 `candidateProjectRoot` 指向一份固定、干净的 TEST 构建工作树。冻结脚本拒绝从日常 DEV 工作树或主工作树构建候选。两个长期 worktree 足够；只有真正需要并行开发时才增加临时 worktree。

安装时可以指定 DEV 源码目录和固定候选构建工作树；三个环境的数据、缓存、TEST Current、PROD beta.8 程序和报告目录在初始部署中固定，安装脚本拒绝覆盖这些路径。PROD beta.8 稳定副本先逐文件校验；入口核对完整 build manifest 和健康接口身份。脚本不写入正式数据目录。

DEV 使用 Python 自动重载和 Vite 热更新。当前机器已有非运行中心管理的 Vite 服务占用 5173，因此固定开发入口采用 5174，原进程保持不动。Vite 代理目标固定读取 `GEWU_DEV_API_URL`，默认 `http://127.0.0.1:8896`，只接受回环 HTTP 的 8896 端口；代理请求要求本地 5174 Host，若带 Origin 则必须是 `localhost` 或 `127.0.0.1` 的 5174 Origin。仓库 Vite 配置不再默认代理正式端口 8765。

DEV/TEST 进程会继承 `GEWU_PROFILE=dev|test` 与 `GEWU_ALLOW_EXTERNAL_EFFECTS=0`。PROD 使用 `GEWU_PROFILE=prod`。DEV/TEST 数据与 PROD 路径相同、服务端口被未知进程占用、构建清单缺失或运行身份不符时，启动器停止并保留现有进程。停止操作仅读取运行中心登记的 PID，并核验可执行文件与命令行身份；身份不符时不会结束该进程。

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
