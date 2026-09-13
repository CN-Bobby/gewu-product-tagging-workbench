# 0.5.0-beta.6 候选验收记录

状态：`pending_acceptance`。本文件记录源码候选，不代表已发布或已完成真实模型语义验收。

## 自动化验收

- 2026-09-13：`.venv\\Scripts\\python.exe -m pytest -q`，`159 passed`。
- 2026-09-13：`npm run build`，Vite production build 通过（1580 modules transformed）。
- 2026-09-13：`scripts/repository_audit.py` 通过；207个受控文件，密钥命中和禁止路径均为0。

|范围|自动证据|仍需人工验收|
|---|---|---|
|已有标签自动画像与混合抽样|`tests/test_source_label_inference.py`|用真实“上衣样式”列核对24张首轮样本分层|
|标准草案、问题回答与原值保护|`tests/test_source_label_inference.py`|核对代表图、冲突样本和编辑体验|
|A→B→A新建原生会话、对话间恢复|`tests/test_runtime_sessions_v8.py`|使用两个真实模型连续切换并核对可见语义|
|coordinator/worker权限边界|`tests/test_runtime_sessions_v8.py`|安装包内确认Reasonix只看到product_workbench|
|模型独立验证与512×512视觉探针|`tests/test_model_settings_validation.py`、`tests/test_runtime_isolation.py`|分别用文本模型、视觉模型及故意错误配置实测|
|schema 7→8升级|`tests/test_schema_v7_migration.py`|在正式数据副本上升级、回退并核对备份|

发布门槛：全量回归和前端构建通过；真实模型、真实商品图片、升级回退及干净 Windows 安装尚未完成前，不创建标签、Release 或对外安装包。
