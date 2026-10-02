# V4 动态标签词典

标签词典在每个任务内根据评论生成，用于同义表达归并、统计和证据检索，不是跨品类固定知识库。

正式产物为 cache/tag_dictionary.json：

~~~json
{
  "schema": "amazon-voc-tag-dictionary/v1",
  "metadata": {
    "model": "模型",
    "prompt_version": "4.0.0",
    "generated_at": "ISO-8601"
  },
  "tags": [{
    "tag_id": "TAG-...",
    "namespace": "pain",
    "canonical_name": "标准名称",
    "definition": "纳入边界",
    "parent_tag_id": "",
    "raw_variants": ["原始表达"],
    "include_examples": ["纳入例"],
    "exclude_examples": ["排除例"],
    "support_event_ids": ["评000001-事01"],
    "independent_review_count": 3,
    "confidence": "高",
    "status": "formal"
  }],
  "mappings": [{
    "event_id": "评000001-事01",
    "field_path": "experience.pain",
    "raw_value": "原始痛点",
    "tag_id": "TAG-..."
  }]
}
~~~

命名空间固定覆盖：

audience / scenario / motivation / task / object / component / attribute / result / pain / itch / desired_outcome / workaround_action / workaround_strategy / journey_action

所有非空可标注字段必须存在逐值映射；标签必须有定义、证据和支持事件，映射必须指向存在的标签。低置信标签可以保留，但标为探索状态。

归并必须同时考虑对象、部件、属性和结果，不能仅凭词面相似。案例：“勒出红印”与“太紧提不上”只有在同样指向袜筒罗纹口弹性不足及相近结果时才可合并；若一个描述穿脱失败，另一个描述长时间压痕，应保留为相邻但不同的结果标签。

固定 L1/L2/L3 树只可作为历史案例，不进入通用执行规则。类 KANO 不能用固定关键词词典生成。
