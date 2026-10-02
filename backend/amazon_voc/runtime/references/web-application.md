# V4 本地 Web 应用

## 运行

```bash
python -m pip install -r requirements.txt
python scripts/run_web.py --host 127.0.0.1 --port 8765
```

`run_web.py` 启动 FastAPI，并以独立进程启动持久 worker。也可分别运行 `python scripts/run_web.py --no-worker` 与 `python -m voc_app.worker`。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VOC_DATA_DIR` | 包目录下 `data` | SQLite、原始上传、revision 和报告目录 |
| `VOC_MODEL_BASE_URL` | `https://api.openai.com/v1` | OpenAI 或兼容接口根地址 |
| `VOC_MODEL_API_KEY` | 空 | 仅从环境读取，不写报告 |
| `VOC_MODEL_PROTOCOL` | `responses` | `responses` 或 `chat_completions` |
| `VOC_EXTRACTOR_MODEL` | `gpt-5-mini` | 评论提取、词典、映射、细分、旅程 |
| `VOC_REVIEWER_MODEL` | `gpt-5.4` | 复核及结构兜底 |
| `VOC_BATCH_REVIEWS` | `40` | 每批最多评论数 |
| `VOC_BATCH_CHARS` | `80000` | 每批最多字符数 |
| `VOC_CONCURRENCY` | `2` | 模型并发数 |

## 单文件输入

每次运行只上传一个 `.xlsx` 或 `.xlsm` 评论工作簿，文件字段为 `reviews`。API 拒绝多个上传文件或不受支持的扩展名。所选项目中的市场、国家、语言、品类是任务设置。工作簿中的评论明细页是唯一数据集；汇总统计不计为评论样本。

## 数据与恢复

- SQLite 保存项目、任务、批次、模型调用、人工修订和产物索引。
- 原始上传按 SHA-256 保存；每次事后修订创建新 `rev-XXXX`，不覆盖旧 revision。
- 批次以 `run_id + revision + stage + batch_id` 幂等，已完成批次可复用。
- worker 重启后重新领取非终态任务，超时 lease 可恢复崩溃任务。
- 状态为 `uploaded → cleaned → extracting → reviewing → normalizing → segmenting → journey → compiling → completed`；失败/取消为 `failed / cancelled`。

## 主要 API

- `GET/POST /api/projects`
- `GET /api/runs`、`POST /api/projects/{project_id}/runs`（单一评论 Excel）
- `GET /api/runs/{run_id}`、取消与重试
- `GET /api/runs/{run_id}/events|segments|journey`
- `PATCH /api/runs/{run_id}/events/{event_id}`：创建新 revision 并从标签整理阶段重算
- `GET /api/runs/{run_id}/artifacts/{html|excel|analysis|quality|calls}`
- `GET/POST /api/settings/test`

本阶段没有应用内用户审批界面。报告检查和反馈通过交付协作完成；明确接受反馈后再据此修订重跑。应用不提供外部来源或搜索 API。
