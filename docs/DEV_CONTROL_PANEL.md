# DEV 开发控制台

开发控制台是独立的 Windows 桌面程序。工作台服务没启动时，它仍能打开并显示 Run Center 的诊断。它不替代 Run Center 的启动、停止、身份及候选校验。

## 安装

在当前 DEV 源码工作树中运行：

```powershell
dotnet publish .\tools\DevControlPanel\GeWuDevControl.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=None -o .\tools\DevControlPanel\publish
powershell -NoProfile -File .\scripts\install-dev-control-panel.ps1
```

安装器要求 Run Center 的 `projectRoot` 与当前源码工作树一致。它在当前用户的 `%LOCALAPPDATA%\GeWuWorkbench\DevControlPanel` 保存按程序哈希区分的版本和旧脚本备份，并新增桌面“格物 · 开发控制台”。现有 DEV／PROD 快捷方式、Run Center 配置、业务数据和服务进程不变。

## 操作

- DEV 状态由 Run Center 的 `-Action Status -Json` 返回。只有进程、端口、健康接口和运行策略身份都通过核对时，才能停止、重启或打开页面；身份冲突时保留现场。
- “热更新 · 允许外部调用”使用 Run Center 的默认联网开发策略；“热更新 · 离线”显式使用离线策略。候选模式只接受指定报告并交给 Run Center 校验，且只能离线运行。
- 停止和重启前会提示正在运行的任务可能中断。打开页面只启动浏览器，不重新校验候选 ZIP。PROD 只显示状态和打开页面，不提供进程控制。
- 重启电脑后若显示“运行异常 / record_without_listener”，可点“清理失效记录”；面板调用 Run Center 再次核对端口和进程后清理旧记录，再选择所需模式启动。其他身份冲突仍禁用控制。
- 点窗口关闭按钮会收起到系统托盘；托盘的“退出控制台”只退出面板，不停止工作台。再次双击桌面入口会唤出已运行的面板。
- “复制诊断”只复制状态摘要与脱敏后的最近错误，不复制进程命令行或凭据。Run Center 原有 PowerShell 入口仍是面板不可用时的备用操作方式。

自动化契约验证：`python -m pytest --noconftest -q tests/test_run_center_status_contract.py`，以及 `dotnet run --project .\tools\DevControlPanel.SelfTest\DevControlPanel.SelfTest.csproj -c Release`。
