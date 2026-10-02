# Amazon VOC 评论洞察

工作台内的独立评论分析页面，接入已验收的 `amazon-voc-analysis` 第一阶段流程。输入、清洗、证据和结论都来自一个评论 Excel；市场、国家、语言和品类由项目设置提供上下文。分析不读取 Alexa、Customer Say、外部补全或其他评论数据。

## 使用流程

1. 在主导航打开“Amazon VOC”。
2. 选择现有 VOC 项目，或新建项目并填写站点、国家、语言和品类。
3. 选择总设置里的文本模型和已授权凭据，上传一个 `.xlsx` 或 `.xlsm` 评论表。
4. 查看任务状态。完成后可打开 HTML 报告、下载 Excel 证据库及查看质量报告。
5. 在任务详情检查评论事件；需要修订时保存修订说明，工作台会创建新 revision 并自动重跑。

HTML 报告包含六个评论洞察模块；Excel 证据库包含 12 个工作表。修订保留原 revision 供对照，下载链接始终对应当前 revision。

## 从 Amazon 评论采集任务启动 VOC

Amazon 评论任务完成并生成 Excel 后，先在“Amazon 评论”页面下载并人工检查该文件。确认文件内容可用于分析时，点击“已检查，送入 VOC 并启动”；此按钮表示用户已检查来源 Excel，并会立即创建 VOC 任务、调用总设置中的默认文本模型及已授权凭据。

每个评论采集任务只关联一个 VOC 项目和任务。VOC 直接读取评论采集任务生成的 Excel，不经过浏览器下载再上传，也不把原始 JSON 作为第二个分析源。重复点击或重试同一来源会打开已有 VOC 任务，不会重复启动模型分析。评论任务部分失败时，页面会提示工作簿只包含已成功采集的评论；人工确认后可按该 Excel 的实际内容启动分析。

## 模型、任务和数据隔离

- VOC 复用“总设置”中现有的文本模型端点、模型名和凭据解析及文本能力验证，不提供第二套 API Key 输入或存储。
- 每个运行只在 VOC 独立 SQLite 中保存模型引用和凭据 ID 快照；API Key 仅在任务进程内用于请求，不写入数据库、报告、API 响应或 VOC 日志。
- 评论原表、缓存、修订和结果存放在工作台 `TAG_STORAGE/amazon-voc/` 下，与商品打标任务数据及既有 Amazon 评论采集数据分开。
- 后台线程在工作台启动时恢复未结束的 VOC 任务，退出时停止调度；可取消排队/执行中任务，并重试失败或已取消任务。
- 只支持一个评论 Excel 文件，限制 50 MiB；站点、国家、语言、品类是项目设置，不是额外分析文件。

## 第二阶段集成测试

这些测试使用本地临时目录和确定性假 provider，不调用 DeepSeek 或其他真实模型，不产生模型费用。

```powershell
scripts\test_amazon_voc_phase2.ps1
```

单独运行：

```powershell
.venv\Scripts\python.exe -m pytest -q tests\test_amazon_voc_workbench.py
npm --prefix frontend run test:amazon-voc
```

后端集成测试覆盖单文件上传、模型身份快照、凭据不落盘、拒绝额外文件、完整分析与报告下载、六模块/12 工作表契约、人工修订重跑、取消重试和启动恢复。前端测试覆盖 API 地址、单文件上传字段、扩展名限制、运行状态和报告交付条件。第一阶段 VOC 自身的回归测试仍独立保留在 Skill 源仓库。

## 集成来源

运行时分析代码来自本机 `Bobby_Skills_Group-voc-phase1-sparse/amazon-voc-analysis` 的已验收工作区，Git 分支 `wip/amz-reviews-analysis-skill-0830-main`、HEAD `1f05a71a0a5ff47385314402b04ebe109dfaa406`。源工作区在复制时包含第一阶段未提交修改；工作台只引入运行时需要的 `voc_app` 核心、提示词、引用资料及流水线脚本，不复制 Phase 1 输入数据和结果。运行时副本位于 `backend/amazon_voc/runtime/`。

本阶段仅完成工作台本地开发接入和确定性测试；不推送 GitHub、不递增客户版本，也不代表绿色测试版/正式版部署或真实模型端到端结果验收。
