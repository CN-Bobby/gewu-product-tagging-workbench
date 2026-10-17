"""Strict-enough JSON schemas for the seven review-analysis model tasks.

Local validation performs the business rules that JSON Schema cannot express.
"""

STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": required or list(properties), "additionalProperties": False}


BI_TEXT = obj({"zh": STRING, "en": STRING})


EVENT = obj({
    "review_id": STRING, "evidence_quote": STRING,
    "audience": obj({"user": STRING, "buyer": STRING, "relationship": STRING,
                     "physical_constraints": STRINGS, "skill_level": STRING, "role_needs": STRINGS}),
    "scenario": obj({"context": STRING, "place": STRING, "environmental_stressors": STRINGS,
                     "social_emotional_context": STRING, "trigger": STRING, "journey_action": STRING}),
    "motivation": obj({"explicit": STRING, "inferred": STRING, "inference_basis": STRINGS}),
    "task": obj({"core_job": STRING, "functional_job": STRING, "emotional_job": STRING,
                 "social_job": STRING, "desired_outcomes": STRINGS}),
    "experience": obj({"object": STRING, "component": STRING, "attribute": STRING,
                       "result": STRING, "pain": STRINGS, "itch": STRINGS,
                       "sentiment": {"type": "string", "enum": ["正面", "负面", "混合", "中性"]},
                       "severity": {"type": "string", "enum": ["高", "中", "低"]}}),
    "workaround": obj({"exists": {"type": "boolean"}, "raw_action": STRING,
                       "standardized_action": STRING, "strategy": STRING,
                       "recovery_goal": STRING, "costs": STRINGS,
                       "effect": {"type": "string", "enum": ["完全解决", "部分解决", "无效", "不明确", ""]}}),
    "fact_inference": {"type": "string", "enum": ["事实", "事实加推理"]},
    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
})

SCHEMAS = {
    "extract_events": obj({"reviews": {"type": "array", "items": obj({
        "review_id": STRING,
        "status": {"type": "string", "enum": ["已生成事件", "无可用需求信号"]},
        "no_signal_reason": STRING,
        "events": {"type": "array", "items": EVENT},
    })}}),
    "review_events": obj({"decisions": {"type": "array", "items": obj({
        "event_id": STRING, "action": {"type": "string", "enum": ["accept", "replace", "drop"]},
        "reason": STRING, "replacement": {"anyOf": [EVENT, {"type": "null"}]},
    })}}),
    "build_dictionary": obj({"tags": {"type": "array", "items": obj({
        "tag_id": STRING, "namespace": STRING, "canonical_name": STRING, "canonical_name_en": STRING,
        "definition": STRING, "definition_en": STRING,
        "parent_tag_id": STRING, "raw_variants": STRINGS, "include_examples": STRINGS,
        "exclude_examples": STRINGS, "support_event_ids": STRINGS,
        "confidence": {"type": "string", "enum": ["高", "中", "低"]},
    }, required=["tag_id", "namespace", "canonical_name", "definition", "parent_tag_id",
                 "raw_variants", "include_examples", "exclude_examples", "support_event_ids", "confidence",
                 "canonical_name_en", "definition_en"])}}),
    "map_dictionary": obj({"mappings": {"type": "array", "items": obj({
        "candidate_id": STRING, "tag_id": STRING,
    })}}),
    "propose_segments": obj({"segments": {"type": "array", "items": obj({
        "segment_id": STRING, "name": STRING, "name_en": STRING,
        "segment_key": obj({"audience": STRING, "scenario": STRING, "motivation": STRING, "task": STRING}),
        "segment_key_en": obj({"audience": STRING, "scenario": STRING, "motivation": STRING, "task": STRING}),
        "persona_summary": STRING, "persona_summary_en": STRING,
        "jtbd_statement": STRING, "jtbd_statement_en": STRING,
        "merge_reason": STRING, "merge_reason_en": STRING,
        "split_reason": STRING, "split_reason_en": STRING,
        "profile_synthesis": obj({
            "users": BI_TEXT, "buyers": BI_TEXT, "relationships": BI_TEXT,
            "physical_constraints": BI_TEXT, "skill_levels": BI_TEXT, "role_needs": BI_TEXT,
            "scenario": BI_TEXT, "motivation": BI_TEXT, "task": BI_TEXT,
            "pains": BI_TEXT, "itches": BI_TEXT, "desired_outcomes": BI_TEXT,
            "product_experience": BI_TEXT, "workarounds": BI_TEXT,
        }),
        "need_priorities": {"type": "array", "items": obj({"need": STRING,
            "priority_type": {"type": "string", "enum": ["类基本型", "类期望型", "类魅力型"]},
            "basis": STRING, "confidence": {"type": "string", "enum": ["高", "中", "低"]}})},
    }, required=["segment_id", "name", "segment_key", "persona_summary", "jtbd_statement", "merge_reason",
                 "split_reason", "need_priorities", "name_en", "segment_key_en", "persona_summary_en",
                 "jtbd_statement_en", "merge_reason_en", "split_reason_en", "profile_synthesis"])}}),
    "assign_segments": obj({"assignments": {"type": "array", "items": obj({
        "event_id": STRING, "segment_id": STRING,
    })}}),
    "build_journey": obj({
        "stages": {"type": "array", "items": obj({"stage_id": STRING, "name": STRING, "name_en": STRING,
            "order": {"type": "integer"}, "definition": STRING, "definition_en": STRING},
            required=["stage_id", "name", "order", "definition", "name_en", "definition_en"])},
        "assignments": {"type": "array", "items": obj({"event_id": STRING, "stage_id": STRING})},
    }),
}
