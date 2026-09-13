# 0.5.0-beta.6 候选验收记录

状态：`pending_acceptance`。候选已构建并在本机从 beta.5 覆盖升级，尚不代表正式发布或真实模型语义验收完成。

## 自动化验收

- 2026-09-13：`.venv\\Scripts\\python.exe -m pytest -q`，`160 passed`。
- 2026-09-13：`npm run build`，Vite production build 通过（1580 modules transformed）。
- 2026-09-13：`scripts/repository_audit.py` 通过；212个受控文件，密钥命中和禁止路径均为0。
- 2026-09-13：已接入的来源列允许再次反推并修订同一标签标准，不复制标签列；对应回归测试通过。

|范围|自动证据|仍需人工验收|
|---|---|---|
|已有标签自动画像与混合抽样|`tests/test_source_label_inference.py`；正式库只读画像为17行已有值、293行空白、8个原始值、310件有图|用真实模型核对24张首轮样本分层与视觉语义|
|标准草案、问题回答与原值保护|`tests/test_source_label_inference.py`|核对代表图、冲突样本和编辑体验|
|A→B→A新建原生会话、对话间恢复|`tests/test_runtime_sessions_v8.py`|使用两个真实模型连续切换并核对可见语义|
|coordinator/worker权限边界|`tests/test_runtime_sessions_v8.py`|安装包内确认Reasonix只看到product_workbench|
|模型独立验证与512×512视觉探针|`tests/test_model_settings_validation.py`、`tests/test_runtime_isolation.py`|分别用文本模型、视觉模型及故意错误配置实测|
|schema 7→8升级|`tests/test_schema_v7_migration.py`；本机 beta.5→beta.6 覆盖升级成功，正式数据仍为1个310行数据集|干净Windows安装与人工回退演练|

## 候选包与本机升级

- 构建提交：`2f7bf2b59975ba2cc5693ec346868d470774fe61`。
- 安装程序：`releases/0.5.0-beta.6/2f7bf2b59975/格物工作台-0.5.0-beta.6-candidate.2f7bf2b59975-安装程序.exe`。
- SHA256：`4a5524d07bcd941085c1676d2630139912ebca6a2f73591e50f75cd37167a8a0`。
- 隔离包内冷启动：内置 Python 3.14.3、schema 8、前端页面加载和浏览器控制台零错误通过；隔离数据目录为 `storage/release-verification/package-smoke-beta6-2f7bf2b59975`。
- 本机安装目录：`K:\\Bobbytagging\\app`；健康接口确认 build ID 为 `0.5.0-beta.6+2f7bf2b59975`。
- beta.5 升级前备份：`K:\\Bobbytagging\\Data\\backups\\upgrade-20260913T095143837778Z`，数据库完整性为 `ok`；旧候选包仍保留在 `releases/0.5.0-beta.5/575333ac1b37`。

发布门槛：源码、包内启动和本机覆盖升级已通过；真实模型语义、真实商品图片抽样质量、人工回退演练及干净 Windows 安装尚未完成前，不创建标签、Release 或客户正式分发包。
