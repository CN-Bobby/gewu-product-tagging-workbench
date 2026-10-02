# V4 模型数据契约

正式模型执行由 `prompts/` 中七个版本化任务和 `voc_app/schemas.py` 中的 JSON Schema 共同约束。优先使用严格 JSON Schema；兼容服务不支持时退回 JSON 模式并做本地结构校验。结构失败最多修复两次，之后改用复核模型；仍失败则停止流水线。

所有模型产物必须包含 `metadata`：`model`、`model_version`、`prompt_version`、`generated_at`。分批执行时追加 `batch_id`；合并后事件 ID 不得改变。

## 1 review_processing.json 与 events.json

`review_processing.json` 为每条清洗后评论记录 `review_id`、`status`、`reason` 和 `event_count`。状态只能是“已生成事件 / 无可用需求信号 / 失败”，不允许静默漏行。

Schema：`amazon-voc-events/v4`

```json
{
  "schema": "amazon-voc-events/v4",
  "metadata": {},
  "events": [{
    "event_id": "评000123-事01",
    "review_id": "评000123",
    "asin": "B0XXXX",
    "rating": 2,
    "review_date": "2026-08-01",
    "review_text": "完整评论原文",
    "evidence_quote": "支撑本事件的最小原文片段",
    "audience": {
      "user": "明确使用者",
      "buyer": "明确购买者",
      "relationship": "Buyer/User关系",
      "physical_constraints": [],
      "skill_level": "",
      "role_needs": []
    },
    "scenario": {
      "context": "动态情境",
      "place": "物理地点",
      "environmental_stressors": [],
      "social_emotional_context": "",
      "trigger": "",
      "journey_action": "用户此时正在做的动作"
    },
    "motivation": {
      "explicit": "评论明确表达的动机",
      "inferred": "分析推理出的动机",
      "inference_basis": ["评000123-事01"]
    },
    "task": {
      "core_job": "解决方案无关的核心任务",
      "functional_job": "",
      "emotional_job": "",
      "social_job": "",
      "desired_outcomes": []
    },
    "experience": {
      "object": "产品对象",
      "component": "部件",
      "attribute": "具体属性",
      "result": "实际结果",
      "pain": [],
      "itch": [],
      "sentiment": "正面|负面|混合|中性",
      "severity": "高|中|低"
    },
    "workaround": {
      "exists": true,
      "raw_action": "用户明确采取的额外动作",
      "standardized_action": "当前任务内归一动作",
      "strategy": "更高层补偿策略",
      "recovery_goal": "试图恢复的目标",
      "costs": [],
      "effect": "完全解决|部分解决|无效|不明确"
    },
    "fact_inference": "事实|事实加推理",
    "confidence": "高|中|低"
  }]
}
```

同一事件一次性完成事实、需求和 Workaround 提取；不得再创建内容重复的 Pass A/B。没有 Workaround 时保留对象并令 `exists=false`，其他字段为空。

## 2 tag_dictionary.json

动态词典覆盖：`audience / scenario / motivation / task / object / component / attribute / result / pain / itch / desired_outcome / workaround_action / workaround_strategy / journey_action`。

每个标签保存 `tag_id`、`namespace`、`canonical_name`、`definition`、`parent_tag_id`、`raw_variants`、`include_examples`、`exclude_examples`、`support_event_ids`、`independent_review_count`、`confidence`、`status`。同文件的 `mappings` 逐条保存 `event_id + field_path + raw_value → tag_id`。

## 3 segment_model.json

Schema：`amazon-voc-segment-model/v3`

```json
{
  "schema": "amazon-voc-segment-model/v3",
  "metadata": {},
  "segments": [{
    "segment_id": "细分-动态名称-8位哈希",
    "name": "可读细分名",
    "status": "formal|exploratory",
    "segment_key": {
      "audience": "人群定义",
      "scenario": "主题场景",
      "motivation": "核心动机",
      "task": "核心任务"
    },
    "member_event_ids": [],
    "evidence_event_ids": [],
    "persona_summary": "基于评论事实的画像摘要",
    "jtbd_statement": "人群+场景+动机+任务+结果",
    "need_priorities": [{
      "need": "需求",
      "priority_type": "类基本型|类期望型|类魅力型",
      "basis": "评论证据和推理依据",
      "confidence": "高|中|低"
    }],
    "merge_reason": "",
    "split_reason": ""
  }],
  "journey": {
    "stages": [{
      "stage_id": "阶段-动态名称-8位哈希",
      "name": "动态阶段名",
      "order": 1,
      "definition": "动作目标、进入条件和结束状态"
    }],
    "event_assignments": [{"event_id": "评000123-事01", "stage_id": "阶段-..."}]
  }
}
```

`segment_key` 必须且只能有四个键。`status` 由独立评论出现频率决定：达到 `max(2, ceil(有效评论数×配置比例))` 为 `formal`，否则为 `exploratory`。

旅程阶段必须从当前任务生成；不得复制美甲、泳装或固定七阶段。所有细分共享阶段主轴，差异由事件映射计算。

## 4 quality_report.json

Schema 为 `amazon-voc-quality/v4`，固定记录七层门禁：结构、处理覆盖、证据、语义、词典、细分与旅程唯一分配、输出一致性。前六层阻断报告编译；第七层在 HTML/Excel 生成后复核，失败则任务不进入 completed。
