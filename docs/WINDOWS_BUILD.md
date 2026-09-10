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

输出位于`releases/应用版本/commit前12位/`，包括安装EXE、发行清单、源代码ZIP、SHA256SUMS和展开的app。发布归档需另附同commit验收记录及外部环境证据。内置第三方许可证保存在app/THIRD_PARTY_LICENSES，运行依赖自身dist-info许可也保留。

安装器按版本使用当前用户下的固定独立程序目录，不提供把程序混装到任意已有目录的选项。卸载会删除本版完整程序目录，包括运行时可能生成的临时字节码；数据、缓存与原会话位于程序目录之外，不进入安装包和源码ZIP，也不会随卸载删除。程序第一次运行选择数据/缓存目录，浏览器完成模型授权配置。

源码commit可复现输入和打包步骤；构建时间、压缩器和编译器时间字段可能使最终EXE字节不同，因此每次构建独立记录校验值，不能声称所有重建EXE天然字节一致。
