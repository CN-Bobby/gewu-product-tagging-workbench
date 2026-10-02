# 评论 Excel 清洗规则

清洗只对唯一输入工作簿的评论明细行去噪与去重，不产生商业判断。统计汇总页、产品评论统计页及其他非评论明细内容不加入样本。清洗动作和计数写入 `clean_log.json`。

| 处理项 | 标准 | 记录要求 |
|---|---|---|
| 空正文 | 删除或隔离 | 记录输入行号与原因 |
| 完全重复 | 按 ASIN、正文及可用日期/标题等稳定字段判定 | 保留首行，记录重复数 |
| 近似重复 | 不自动删除 | 标记为待检查，避免误删相似但独立的评论 |
| UI/HTML 噪声 | 去除明显的标签、按钮和导航残片 | 保留原始评论供审计 |
| 低信息评论 | 如 “Love it” 仍保留 | 可标注信息密度低 |
| 语言 | 保留工作簿原始文本 | 不擅自翻译或改写 |
| 缺失元数据 | 保持空值 | 不从工作簿汇总或外部来源填补 |

清洗后每行包含规范字段：`review_id`、`asin`、`title`、`rank`、`url`、`sentiment`、`rating`、`review_title`、`content`、`variant`、`review_date`。标题或元数据缺失不阻止分析；ASIN 或正文无法识别属于输入错误。

```json
{"review_id":"R-000001","asin":"B0XXXX","review_title":"...","rating":4,
 "review_date":"2026-06-07","variant":"Color=Mint Green","content":"原文（不改写）",
 "language":"en","dup_flag":"","info_density_hint":""}
```

完全重复去重记录“保留 1 条 + 删除 N 条”。清洗前后有效行数从明细数据计算。没有 Alexa QA、Customer Say 或外部文件清洗阶段。
