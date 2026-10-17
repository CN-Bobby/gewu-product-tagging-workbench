# Windows可复现构建

源码使用本地Git，构建禁止脏工作区，禁止覆盖已有产物目录。正式发布要求VERSION.json的release_status为accepted；未验收只能用明确的候选构建。

开发环境需Windows x64、Python3.14、Node及系统.NET Framework C#编译器。客户安装后不需要开发环境。源码包含npm锁文件；运行时依赖单独锁定完整extras依赖闭包及Windows wheel SHA256，避免把开发机全局包打入产品。

```powershell
python -m pip install -r requirements.txt
npm ci --prefix frontend
python scripts/fetch_build_inputs.py
python -m pytest -q
python scripts/build_windows.py --candidate
```

构建输入由build-tools.lock.json锁定：Python3.14.3 embeddable、Reasonix1.25.1原生Windows二进制、NSIS3.12、Reasonix MIT许可。下载后先校验再使用；任何字节变化都拒绝。

## 开发测试构建（不安装）

日常迭代不走安装包。固定测试入口是桌面“格物工作台（测试版）”。`开发测试工作台.cmd`也会从`builds/current.json`读取固定指针并启动`K:\GeWuTest\Current`。`构建测试版本.cmd`（`scripts\build-test.ps1`）只构建干净工作区的候选到`builds\应用版本+commit前12位\`；它不改current指针、不启动候选，也不自动替换固定测试版。候选必须按下文的升级步骤核验、备份和部署。

开发构建跳过发布门禁与NSIS，不产出安装包、源码ZIP或校验文件；已存在的构建目录一律拒绝覆盖：拒绝只发生在工作区状态与上次构建完全相同时，提示会说明该目录已存在、删掉才能重建。

数据目录完全在程序目录之外。2026-09-25 已将原`E:\GeWuTest`完整复制到`K:\GeWuTest`并逐文件SHA-256核验；原E盘目录作为迁移前快照保留，不作为日常启动入口。固定绿色测试环境为`K:\GeWuTest`：`Current\app`放当前程序，`Data`和`Cache`独立保存，`Rollback\Previous\app`只放上一版程序。测试入口固定使用`K:\GeWuTest\Current`、`K:\GeWuTest\Data`、`K:\GeWuTest\Cache`和专用端口8895；正式安装端口8765不用于测试。Launcher把安装版记录在`%LOCALAPPDATA%\GeWuWorkbench\desktop.json`的数据目录只读地当拒绝清单：以开发模式打开该目录会被拒绝，正式验收必须显式加`-Acceptance`；该文件存在但读不出内容时同样拒绝启动，不猜测哪个目录安全。

### 固定绿色测试环境操作

首次测试版本已由干净提交`35245440b9c2d7a8238035eb646a39fcfc0de17a`构建，`build_id=0.5.0-beta.7+35245440b9c2`、`dirty=false`。桌面入口“格物工作台（测试版）”通过`scripts\dev-launcher.ps1`启动`K:\GeWuTest\Current`，并显式绑定测试数据、缓存和端口8895。`builds\current.json`也指向`K:\GeWuTest\Current`；`dev-launcher.ps1`的默认数据、缓存和端口均为固定K盘测试环境。正式桌面与开始菜单快捷方式保持指向`K:\Bobbytagging\app\Workbench.exe`。

日常升级不制作安装程序。使用与目标提交一致的干净 Git worktree，确认`git status --porcelain`为空，然后运行`python scripts\build_windows.py --dev`。该命令只生成`builds\<build_id>\app`，不会生成安装包；构建使用的`storage\build-tools`和`frontend\node_modules`需按本机准备。只接受`app\build-manifest.json`中版本、commit、build_id与预期一致且`dirty=false`的候选。`build-test.ps1`现只生成候选，不再更新current指针或启动候选；工作区不干净时构建脚本会拒绝执行。

如需从现有仓库建立干净构建工作树，可按下面方式固定到目标提交；`$sha`必须是用户选定的真实提交，不要填入示例或猜测值：

```powershell
$sha = (git -C 'D:\数据打标项目' rev-parse origin/feature/0.5.0-delivery).Trim()
$source = Join-Path $env:TEMP "GeWu-CleanBuild-$sha"
git worktree add --detach $source $sha
New-Item -ItemType Junction (Join-Path $source 'storage\build-tools') -Target 'D:\数据打标项目\storage\build-tools'
New-Item -ItemType Junction (Join-Path $source 'frontend\node_modules') -Target 'D:\数据打标项目\frontend\node_modules'
& 'D:\数据打标项目\.venv\Scripts\python.exe' (Join-Path $source 'scripts\build_windows.py') --dev
```

该worktree的构建目录是候选暂存来源；不要改`builds\current.json`来跳过备份步骤。部署完成并确认恢复点有效后，可以按Git worktree流程移除临时源码目录。

升级步骤：

1. 复制候选`app`到工作目录之外的临时目录，并核对`build-manifest.json`及全部相对路径/文件 SHA-256；候选验证通过前不停止当前实例。
2. 用旧版入口停止测试实例并确认端口8895释放：`powershell -NoProfile -ExecutionPolicy Bypass -File D:\数据打标项目\scripts\dev-launcher.ps1 -Build K:\GeWuTest\Current -Storage K:\GeWuTest\Data -Cache K:\GeWuTest\Cache -Port 8895 -Stop`。
3. 将`Current`程序目录移至`Rollback\Previous`。若已有`Previous`，先按其manifest生成归档清单并移入`E:\GeWuArchive`；不要覆盖测试数据或缓存。
4. 将已校验候选放到`Current\app`，启动并检查`/api/health`中的`version`、`build_id`、`commit`、`build_mode`、`dirty`和`storage`，确认`storage=K:\GeWuTest\Data`。缓存参数固定为`K:\GeWuTest\Cache`。
5. 启动失败时，将失败候选移入`E:\GeWuArchive`留存，再把`Rollback\Previous`恢复为`Current`并用相同测试数据/缓存/端口启动。`Previous`只回退程序文件；如数据 schema 已迁移，按该版本对应的数据备份恢复，不能只替换程序。依据见`docs\UPGRADE_ROLLBACK.md`。

停止测试版也必须显式给出同一组`-Build`、`-Storage`、`-Cache`和`-Port`参数。首次启动会在`K:\GeWuTest\Data`初始化独立数据库；不要把正式`K:\Bobbytagging\Data`、`Data-Test`、provider凭据、数据库或缓存复制进新环境。DEV/TEST 默认允许外部调用；人工验收启动不再要求弹窗选择，自动化脚本继续用隔离数据、假 provider 或网络拦截。

app内的build-manifest.json记录版本、build_id、commit、branch、build_mode（dev/candidate/release）、storage_mode、dirty、built_at，脏构建另记录决定目录名的`worktree_sha256`。顶栏显示实际运行的build_id，开发构建或未提交工作区另有标记；「模型设置→版本技术信息」列出版本、build_id、release_status、commit、branch、build_mode、storage_mode、dirty与built_at（`worktree_sha256`只在manifest内，用于对应目录名）。

安装包只用于Release Candidate、最终Real Acceptance、正式交付，以及安装/升级机制本身的专项测试。

输出位于`releases/应用版本/commit前12位/`，包括安装EXE、发行清单、源代码ZIP、SHA256SUMS和展开的app。发布归档需另附同commit验收记录及外部环境证据。内置第三方许可证保存在app/THIRD_PARTY_LICENSES，运行依赖自身dist-info许可也保留。

安装器显示标准目录选择页，首次默认使用`%LOCALAPPDATA%\GeWuWorkbench\app`，也可选择D盘等当前用户可写目录；升级从注册表复用原位置。安装器不申请管理员权限，选择`Program Files`等受保护目录时会提示改用普通目录。客户电脑只保留一个稳定快捷方式和卸载项。卸载会删除当前程序目录，包括运行时可能生成的临时字节码；数据、缓存与原会话位于程序目录之外，不进入安装包和源码ZIP，也不会随卸载删除。程序第一次运行选择数据/缓存目录，浏览器完成模型授权配置。

源码commit可复现输入和打包步骤；构建时间、压缩器和编译器时间字段可能使最终EXE字节不同，因此每次构建独立记录校验值，不能声称所有重建EXE天然字节一致。
