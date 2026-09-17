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

日常迭代不走安装包。`构建测试版本.cmd`（等价于`scripts\build-test.ps1`）一次完成：读VERSION.json版本号、取git短SHA、执行`build_windows.py --dev --allow-dirty`、输出到`builds/应用版本+commit前12位/`（脏工作区再加`-dirty.工作区摘要前8位`；该摘要覆盖全部已跟踪改动与未跟踪新文件，所以只改`scripts/`或前端依赖也会得到独立目录，不会与上次构建撞名）、写入`builds/current.json`指针、启动该版本。`开发测试工作台.cmd`（`scripts\dev-launcher.ps1`）是固定入口，只读current指针，切换版本只改指针，不需要重新安装或卸载。

开发构建跳过发布门禁与NSIS，不产出安装包、源码ZIP或校验文件；已存在的构建目录一律拒绝覆盖：拒绝只发生在工作区状态与上次构建完全相同时，提示会说明该目录已存在、删掉才能重建。历史版本原样保留用于回退和A/B对比。

数据目录完全在程序目录之外。开发构建默认使用显式指定的`K:\Bobbytagging\Data-Test`，端口按数据目录推导（8765保留给安装版），因此删除任意一个build目录都不影响业务数据，运行期也不会向程序目录写入数据库、日志或缓存。Launcher把安装版记录在`%LOCALAPPDATA%\GeWuWorkbench\desktop.json`的数据目录只读地当拒绝清单：以开发模式打开该目录会被拒绝，正式验收必须显式加`-Acceptance`；该文件存在但读不出内容时同样拒绝启动，不猜测哪个目录安全。

app内的build-manifest.json记录版本、build_id、commit、branch、build_mode（dev/candidate/release）、storage_mode、dirty、built_at，脏构建另记录决定目录名的`worktree_sha256`。顶栏显示实际运行的build_id，开发构建或未提交工作区另有标记；「模型设置→版本技术信息」列出版本、build_id、release_status、commit、branch、build_mode、storage_mode、dirty与built_at（`worktree_sha256`只在manifest内，用于对应目录名）。

安装包只用于Release Candidate、最终Real Acceptance、正式交付，以及安装/升级机制本身的专项测试。

输出位于`releases/应用版本/commit前12位/`，包括安装EXE、发行清单、源代码ZIP、SHA256SUMS和展开的app。发布归档需另附同commit验收记录及外部环境证据。内置第三方许可证保存在app/THIRD_PARTY_LICENSES，运行依赖自身dist-info许可也保留。

安装器显示标准目录选择页，首次默认使用`%LOCALAPPDATA%\GeWuWorkbench\app`，也可选择D盘等当前用户可写目录；升级从注册表复用原位置。安装器不申请管理员权限，选择`Program Files`等受保护目录时会提示改用普通目录。客户电脑只保留一个稳定快捷方式和卸载项。卸载会删除当前程序目录，包括运行时可能生成的临时字节码；数据、缓存与原会话位于程序目录之外，不进入安装包和源码ZIP，也不会随卸载删除。程序第一次运行选择数据/缓存目录，浏览器完成模型授权配置。

源码commit可复现输入和打包步骤；构建时间、压缩器和编译器时间字段可能使最终EXE字节不同，因此每次构建独立记录校验值，不能声称所有重建EXE天然字节一致。
