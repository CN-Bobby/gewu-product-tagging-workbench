# -*- coding: utf-8 -*-
"""Validate the V4 evidence and single-source segmentation contracts."""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


EVENT_SCHEMAS = {"amazon-voc-events/v3", "amazon-voc-events/v4"}
SEGMENT_SCHEMA = "amazon-voc-segment-model/v3"
SEGMENT_KEYS = {"audience", "scenario", "motivation", "task"}
METADATA_FIELDS = {"model", "model_version", "prompt_version", "generated_at"}


def _load(workdir, name, default=None):
    path = common.cache_path(workdir, name)
    return common.read_json(path) if os.path.exists(path) else default


def _minimum_reviews(master, cleaned):
    total = len(cleaned.get("rows") or [])
    config = master.get("analysis_config") or {}
    rate = float(config.get("segment_min_review_rate", 0.005) or 0.005)
    floor = int(config.get("segment_min_review_count", 2) or 2)
    return max(floor, int(math.ceil(total * rate)))


def validate_methodology(workdir):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": str(detail)})

    master = _load(workdir, "master.json", {}) or {}
    cleaned = _load(workdir, "cleaned_reviews.json", {"rows": []}) or {"rows": []}
    events_doc = _load(workdir, "events.json", {}) or {}
    model = _load(workdir, "segment_model.json", {}) or {}
    events = events_doc.get("events") or []
    segments = model.get("segments") or []
    journey = model.get("journey") or {}

    add("评论为唯一必需输入", bool(cleaned.get("rows")), "有效评论=%s" % len(cleaned.get("rows") or []))
    add("事件Schema", events_doc.get("schema") in EVENT_SCHEMAS, events_doc.get("schema"))
    event_meta_missing = METADATA_FIELDS - set(key for key, value in (events_doc.get("metadata") or {}).items() if value)
    add("事件生成元数据完整", not event_meta_missing, sorted(event_meta_missing))
    event_ids = [item.get("event_id") for item in events]
    add("事件ID唯一", bool(event_ids) and len(event_ids) == len(set(event_ids)) and all(event_ids), "事件=%s" % len(event_ids))
    bad_trace = [item.get("event_id") for item in events
                 if not item.get("review_id") or not item.get("asin") or not item.get("evidence_quote")]
    add("事件证据可回溯", not bad_trace, "违规=%s" % bad_trace[:8])

    required_groups = {"audience", "scenario", "motivation", "task", "experience", "workaround"}
    bad_groups = [item.get("event_id") for item in events if not required_groups <= set(item)]
    add("事件维度完整", not bad_groups, "违规=%s" % bad_groups[:8])
    bad_inference = [item.get("event_id") for item in events
                     if (item.get("motivation") or {}).get("inferred")
                     and not (item.get("motivation") or {}).get("inference_basis")]
    add("推理均有依据", not bad_inference, "违规=%s" % bad_inference[:8])

    add("细分Schema", model.get("schema") == SEGMENT_SCHEMA, model.get("schema"))
    model_meta_missing = METADATA_FIELDS - set(key for key, value in (model.get("metadata") or {}).items() if value)
    add("细分生成元数据完整", not model_meta_missing, sorted(model_meta_missing))
    segment_ids = [item.get("segment_id") for item in segments]
    add("单一细分ID唯一", bool(segment_ids) and len(segment_ids) == len(set(segment_ids)) and all(segment_ids), segment_ids[:8])
    bad_keys = [item.get("segment_id") for item in segments
                if set(item.get("segment_key") or {}) != SEGMENT_KEYS
                or not all(common.clean_text((item.get("segment_key") or {}).get(key)) for key in SEGMENT_KEYS)]
    add("细分键仅含人群场景动机任务", not bad_keys, "违规=%s" % bad_keys[:8])

    event_set = set(event_ids)
    minimum = _minimum_reviews(master, cleaned)
    bad_members = []
    bad_status = []
    membership = {}
    for segment in segments:
        members = segment.get("member_event_ids") or []
        if not members or not set(members) <= event_set:
            bad_members.append(segment.get("segment_id"))
            continue
        for event_id in members:
            membership.setdefault(event_id, []).append(segment.get("segment_id"))
        member_events = [item for item in events if item.get("event_id") in set(members)]
        reviews = common.independent_review_ids(member_events)
        expected = "formal" if len(reviews) >= minimum else "exploratory"
        if segment.get("status") != expected:
            bad_status.append({"segment_id": segment.get("segment_id"), "expected": expected,
                               "actual": segment.get("status"), "reviews": len(reviews)})
    add("细分成员存在", not bad_members, "违规=%s" % bad_members[:8])
    add("正式与探索状态仅由评论频率决定", not bad_status, "最低评论=%s; 违规=%s" % (minimum, bad_status[:5]))
    duplicate_members = {key: value for key, value in membership.items() if len(value) != 1}
    unassigned = sorted(event_set - set(membership))
    add("每个事件只进入一个细分", not duplicate_members and not unassigned,
        "重复=%s; 未分配=%s" % (list(duplicate_members.items())[:5], unassigned[:8]))
    incomplete = [item.get("segment_id") for item in segments
                  if not item.get("name") or not item.get("persona_summary") or not item.get("jtbd_statement")
                  or not item.get("merge_reason") or not item.get("split_reason")]
    add("细分解释字段完整", not incomplete, "违规=%s" % incomplete[:8])

    stages = journey.get("stages") or []
    assignments = journey.get("event_assignments") or []
    stage_ids = [item.get("stage_id") for item in stages]
    add("旅程阶段动态且ID唯一", bool(stage_ids) and len(stage_ids) == len(set(stage_ids)), stage_ids)
    bad_stage = [item for item in assignments
                 if item.get("event_id") not in event_set or item.get("stage_id") not in set(stage_ids)]
    add("旅程映射可回溯", not bad_stage, "违规=%s" % bad_stage[:5])
    assignment_ids = [item.get("event_id") for item in assignments]
    add("每个事件只映射一个旅程阶段",
        len(assignment_ids) == len(set(assignment_ids)) and set(assignment_ids) == event_set,
        "映射=%s/%s" % (len(set(assignment_ids)), len(event_set)))
    bad_stage_shape = [item.get("stage_id") for item in stages
                       if not item.get("name") or item.get("order") in (None, "")
                       or not item.get("definition")]
    add("旅程阶段定义完整", not bad_stage_shape, "违规=%s" % bad_stage_shape[:8])

    passed = sum(1 for item in checks if item["pass"])
    report = {
        "schema": "amazon-voc-methodology-validation/v3",
        "passed": passed,
        "total": len(checks),
        "all_green": passed == len(checks),
        "minimum_formal_segment_reviews": minimum,
        "checks": checks,
    }
    common.write_json(common.cache_path(workdir, "methodology_validation.json"), report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    report = validate_methodology(args.workdir)
    for item in report["checks"]:
        print("[validate-method] %s %s - %s" % ("PASS" if item["pass"] else "FAIL", item["check"], item["detail"]))
    if not report["all_green"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
