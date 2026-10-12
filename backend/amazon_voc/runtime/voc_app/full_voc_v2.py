from __future__ import annotations

import copy
import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any


PROMPT_VERSION = "amazon-voc-full-v2.7"
MIN_FORMAL_SEGMENT_REVIEWS = 5
FORMAL_SEGMENT_RATE = 0.01
MIN_FORMAL_SEGMENT_EVIDENCE_REVIEWS = 3
MAX_DICTIONARY_CANDIDATES = 120
DICTIONARY_CANDIDATE_COVERAGE = 0.8

FIELD_NAMESPACES = {
    "audience.user": "audience",
    "scenario.context": "scenario",
    "motivation.explicit": "motivation",
    "motivation.inferred": "motivation",
    "task.core_job": "task",
    "task.desired_outcome": "desired_outcome",
    "experience.object": "object",
    "experience.result": "result",
    "experience.pain": "pain",
    "experience.itch": "itch",
    "workaround.action": "workaround_action",
}

INSUFFICIENT = {"zh": "证据不足", "en": "Insufficient evidence"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _quote_matches_source(quote: str, searchable: str) -> bool:
    normalize = lambda value: " ".join(_text(value).split()).casefold()
    normalized_quote = normalize(quote)
    return bool(normalized_quote) and normalized_quote in normalize(searchable)


def _pair(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        zh, en = _text(value.get("zh")), _text(value.get("en"))
        if zh and en:
            return {"zh": zh, "en": en}
    return dict(INSUFFICIENT)


def _has_bilingual(value: Any) -> bool:
    return isinstance(value, dict) and bool(_text(value.get("zh"))) and bool(_text(value.get("en")))


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _values_at(event: dict, path: str):
    value: Any = event
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    # Mapping values must match the event value byte-for-byte; whitespace is evidence too.
    return [item for item in values if isinstance(item, str) and item.strip()]


def normalize_mapping_paths(result: dict) -> dict:
    """Repair unknown paths only when event value and tag namespace identify one exact path."""
    normalized = copy.deepcopy(result)
    events = normalized.get("events") or []
    mappings = normalized.get("event_tag_mappings") or []
    tags = normalized.get("tag_dictionary") or []
    tag_by_id = {row.get("tag_id"): row for row in tags if row.get("tag_id")}

    expected = set()
    for event in events:
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                expected.add((event.get("event_id"), field_path, raw_value))

    occupied = set()
    unknown_rows = []
    for index, mapping in enumerate(mappings):
        field_path = mapping.get("field_path")
        key = (mapping.get("event_id"), field_path, mapping.get("raw_value"))
        if field_path in FIELD_NAMESPACES:
            if key in expected:
                occupied.add(key)
        else:
            unknown_rows.append(index)

    candidates_by_index = {}
    claims = Counter()
    for index in unknown_rows:
        mapping = mappings[index]
        event_id = mapping.get("event_id")
        raw_value = mapping.get("raw_value")
        tag = tag_by_id.get(mapping.get("tag_id")) or {}
        namespace = tag.get("namespace")
        candidates = [
            (event_id, field_path, raw_value)
            for field_path, expected_namespace in FIELD_NAMESPACES.items()
            if expected_namespace == namespace
            and (event_id, field_path, raw_value) in expected
            and (event_id, field_path, raw_value) not in occupied
        ]
        candidates_by_index[index] = candidates
        for candidate in candidates:
            claims[candidate] += 1

    # Resolve as a batch so duplicate unknown rows cannot greedily claim one expected relation.
    for index, candidates in candidates_by_index.items():
        if len(candidates) == 1 and claims[candidates[0]] == 1:
            mappings[index]["field_path"] = candidates[0][1]
    return normalized


def normalize_v2_result(result: dict) -> dict:
    """Apply safe local derivations before the complete V2 contract is validated."""
    normalized = normalize_mapping_paths(result)
    event_counts = Counter(row.get("sample_id") for row in (normalized.get("events") or []))
    for row in normalized.get("review_processing") or []:
        if "event_count" not in row:
            row["event_count"] = event_counts.get(row.get("sample_id"), 0)
    return normalized


def minimum_formal_segment_reviews(review_count: int) -> int:
    return max(MIN_FORMAL_SEGMENT_REVIEWS, math.ceil(max(0, int(review_count)) * FORMAL_SEGMENT_RATE))


def build_dictionary_candidates(events: list[dict], max_candidates: int = MAX_DICTIONARY_CANDIDATES) -> dict:
    """Select frequent field values for bounded semantic normalization; leave the long tail local."""
    reviews_by_value: dict[tuple[str, str], set[str]] = defaultdict(set)
    events_by_value: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events or []:
        event_id = _text(event.get("event_id"))
        sample_id = _text(event.get("sample_id"))
        if not event_id or not sample_id:
            continue
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                key = (field_path, raw_value)
                reviews_by_value[key].add(sample_id)
                events_by_value[key].add(event_id)

    total_review_value_occurrences = sum(len(ids) for ids in reviews_by_value.values())
    by_namespace: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, sample_ids in reviews_by_value.items():
        if len(sample_ids) >= 2:
            by_namespace[FIELD_NAMESPACES[key[0]]].append(key)

    candidates = []
    total_candidate_occurrences = 0
    remaining = max(0, int(max_candidates))
    selected_review_occurrences: dict[str, int] = defaultdict(int)
    eligible_review_occurrences = {
        namespace: sum(len(reviews_by_value[key]) for key in keys)
        for namespace, keys in by_namespace.items()
    }
    ranked = sorted(
        [(namespace, key) for namespace, keys in by_namespace.items() for key in keys],
        key=lambda item: (-len(reviews_by_value[item[1]]), -len(events_by_value[item[1]]),
                          item[0], item[1][1].casefold(), item[1][0]),
    )
    completed_namespaces = set()
    for namespace in by_namespace:
        if not eligible_review_occurrences[namespace]:
            completed_namespaces.add(namespace)
    for namespace, key in ranked:
        if remaining <= 0 or len(completed_namespaces) == len(by_namespace):
            break
        if namespace in completed_namespaces:
            continue
        field_path, raw_value = key
        digest = hashlib.sha256((field_path + "\0" + raw_value).encode("utf-8")).hexdigest()[:16]
        support = len(reviews_by_value[key])
        candidates.append({
            "candidate_id": "CAND-" + digest,
            "namespace": namespace,
            "field_path": field_path,
            "raw_value": raw_value,
            "review_count": support,
            "event_count": len(events_by_value[key]),
        })
        selected_review_occurrences[namespace] += support
        total_candidate_occurrences += support
        remaining -= 1
        if selected_review_occurrences[namespace] / eligible_review_occurrences[namespace] >= DICTIONARY_CANDIDATE_COVERAGE:
            completed_namespaces.add(namespace)
    denominator = sum(eligible_review_occurrences.values())
    candidate_namespace_counts = Counter(row["namespace"] for row in candidates)
    return {
        "candidates": candidates,
        "total_unique_values": len(reviews_by_value),
        "candidate_count": len(candidates),
        "mergeable_candidate_count": sum(count for count in candidate_namespace_counts.values() if count >= 2),
        "review_value_occurrences": denominator,
        "all_review_value_occurrences": total_review_value_occurrences,
        "candidate_review_value_occurrences": total_candidate_occurrences,
        "candidate_coverage": (total_candidate_occurrences / denominator) if denominator else 0.0,
    }


def validate_dictionary_normalization(payload: dict, result: dict) -> list[str]:
    """Validate only candidate-to-concept joins; omitted candidates safely remain raw."""
    candidates = payload.get("candidates") or []
    by_id = {row.get("candidate_id"): row for row in candidates if row.get("candidate_id")}
    groups = result.get("groups") if isinstance(result, dict) else None
    if not isinstance(groups, list):
        return ["词项归并结果必须包含 groups 数组"]
    seen = set()
    problems = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            problems.append("归并组 %d 不是对象" % index)
            continue
        ids = group.get("candidate_ids")
        if (not isinstance(ids, list) or len(ids) < 2
                or any(not isinstance(candidate_id, str) for candidate_id in ids)
                or len(ids) != len(set(ids))):
            problems.append("归并组 %d 必须引用至少两个互不重复的候选 ID" % index)
            continue
        if any(candidate_id not in by_id for candidate_id in ids):
            problems.append("归并组 %d 引用了输入中不存在的候选 ID" % index)
            continue
        namespaces = {by_id[candidate_id].get("namespace") for candidate_id in ids}
        if len(namespaces) != 1 or group.get("namespace") not in namespaces:
            problems.append("归并组 %d 混用了不同词典命名空间" % index)
        if seen.intersection(ids):
            problems.append("归并组 %d 与其他组重复使用候选 ID" % index)
        seen.update(ids)
        for name in ("canonical_name", "canonical_name_en", "definition", "definition_en"):
            if not _text(group.get(name)):
                problems.append("归并组 %d 缺少 %s" % (index, name))
    return list(dict.fromkeys(problems))


def build_payload(master: dict, cleaned_rows: list[dict]) -> dict:
    """Build the one-call V2 input from the uploaded and locally cleaned workbook only."""
    rows = []
    for row in cleaned_rows:
        sample_id = _text(row.get("review_id"))
        if not sample_id:
            continue
        rows.append({
            "sample_id": sample_id,
            "asin": _text(row.get("asin")),
            "rating": row.get("rating", ""),
            "review_date": _text(row.get("review_date")),
            "variant": _text(row.get("variant")),
            "title": _text(row.get("review_title")),
            "text": _text(row.get("content")),
        })
    return {
        "sample_count": len(rows),
        "task_settings": {
            "task_id": _text(master.get("task_id")),
            "marketplace": _text(master.get("marketplace")),
            "country": _text(master.get("country")),
            "language": _text(master.get("language")),
            "category": _text(master.get("category")),
        },
        "reviews": rows,
    }


def build_synthesis_payload(payload: dict, events: list[dict]) -> dict:
    """Retain full local evidence for validation and deterministic report assembly."""
    return {
        "sample_count": int(payload.get("sample_count") or 0),
        "task_settings": dict(payload.get("task_settings") or {}),
        "reviews": [{"sample_id": row.get("sample_id"), "asin": row.get("asin", "")}
                    for row in payload.get("reviews") or []],
        "events": copy.deepcopy(events),
    }


def compact_synthesis_request(payload: dict, *, include_evidence_quotes: bool = True) -> dict:
    """Send only atomic evidence fields to the model; keep full events local for joins."""
    compact_events = []
    for event in payload.get("events") or []:
        row = {
            "event_id": event.get("event_id"),
            "sample_id": event.get("sample_id"),
            "primary_for_review": event.get("primary_for_review") is True,
        }
        for source_key, target_key, fields in (
            ("audience", "audience", ("user",)),
            ("scenario", "scenario", ("context",)),
            ("motivation", "motivation", ("explicit", "inferred")),
            ("task", "task", ("core_job", "desired_outcome")),
            ("experience", "experience", ("object", "result", "pain", "itch")),
            ("workaround", "workaround", ("action",)),
        ):
            source = event.get(source_key) if isinstance(event.get(source_key), dict) else {}
            selected = {key: source.get(key) for key in fields
                        if isinstance(source.get(key), str) and source.get(key).strip()}
            if selected:
                row[target_key] = selected
        if include_evidence_quotes and event.get("evidence_quote"):
            row["evidence_quote"] = event["evidence_quote"]
        if event.get("confidence"):
            row["confidence"] = event["confidence"]
        compact_events.append(row)
    return {
        "sample_count": int(payload.get("sample_count") or 0),
        "asin_count": len({row.get("asin") for row in payload.get("reviews") or [] if row.get("asin")}),
        "asins": sorted({row.get("asin") for row in payload.get("reviews") or [] if row.get("asin")}),
        "task_settings": dict(payload.get("task_settings") or {}),
        "events": compact_events,
    }


def build_assignment_jobs(payload: dict, synthesis: dict, batch_size: int = 100) -> list[tuple[str, dict]]:
    """Split event assignment from global synthesis and keep each response bounded."""
    compact = compact_synthesis_request(payload, include_evidence_quotes=False)
    segments = [{key: segment.get(key) for key in ("segment_id", "segment_key", "name", "synthesis", "job")}
                for segment in synthesis.get("segments") or []]
    stages = [{key: stage.get(key) for key in ("stage_id", "name", "synthesis", "user_need", "friction_or_outcome")}
              for stage in (synthesis.get("journey") or {}).get("stages") or []]
    size = max(1, int(batch_size))
    jobs = []
    for index in range(0, len(compact["events"]), size):
        rows = compact["events"][index:index + size]
        batch_id = "assign-%04d" % (index // size + 1)
        jobs.append((batch_id, {
            "sample_count": compact["sample_count"],
            "segments": copy.deepcopy(segments),
            "journey_stages": copy.deepcopy(stages),
            "events": rows,
        }))
    return jobs


def validate_assignment_batch(payload: dict, result: dict) -> list[str]:
    """Require a one-to-one assignment for only the event IDs in this bounded batch."""
    expected = [row.get("event_id") for row in payload.get("events") or []]
    actual = [row.get("event_id") for row in result.get("event_assignments") or []]
    problems = []
    if (not all(expected) or len(set(expected)) != len(expected)
            or len(actual) != len(expected) or set(actual) != set(expected)
            or len(set(actual)) != len(actual)):
        problems.append("批次事件必须在 event_assignments 中恰好归属一次")
    segment_ids = {row.get("segment_id") for row in payload.get("segments") or []}
    stage_ids = {row.get("stage_id") for row in payload.get("journey_stages") or []}
    for row in result.get("event_assignments") or []:
        if row.get("segment_id") not in segment_ids | {"OTHER"}:
            problems.append("事件 %s 的细分归属无效" % row.get("event_id"))
            break
        if row.get("journey_stage_id") not in stage_ids | {"JOURNEY_UNCLEAR"}:
            problems.append("事件 %s 的旅程归属无效" % row.get("event_id"))
            break
    return problems


def repair_assignment_batch(payload: dict, result: dict) -> tuple[dict, dict[str, int]]:
    """Repair assignment coverage gaps by ID without fabricating semantic mappings."""
    expected = [row.get("event_id") for row in payload.get("events") or []]
    segment_ids = {row.get("segment_id") for row in payload.get("segments") or []}
    stage_ids = {row.get("stage_id") for row in payload.get("journey_stages") or []}
    buckets: dict[str, list[dict]] = defaultdict(list)
    summary = {"missing": 0, "duplicate": 0, "invalid_path": 0, "unknown": 0}
    for row in result.get("event_assignments") or []:
        event_id = row.get("event_id")
        if event_id not in set(expected):
            summary["unknown"] += 1
        else:
            buckets[event_id].append(row)
    assignments = []
    for event_id in expected:
        candidates = buckets.get(event_id, [])
        if len(candidates) > 1:
            summary["duplicate"] += len(candidates) - 1
        valid = [row for row in candidates
                 if row.get("segment_id") in segment_ids | {"OTHER"}
                 and row.get("journey_stage_id") in stage_ids | {"JOURNEY_UNCLEAR"}]
        if len(candidates) == 1 and len(valid) == 1:
            assignments.append(dict(valid[0]))
            continue
        if candidates and len(valid) != len(candidates):
            summary["invalid_path"] += 1
        if not candidates:
            summary["missing"] += 1
        assignments.append({
            "event_id": event_id,
            "segment_id": "OTHER",
            "journey_stage_id": "JOURNEY_UNCLEAR",
            "confidence": "low",
        })
    return {"event_assignments": assignments}, summary


def validate_synthesis_semantics(payload: dict, result: dict) -> list[str]:
    """Validate stable semantic IDs before checkpointing synthesis.

    Evidence IDs are intentionally sanitized during local assembly: one stale citation
    must not discard the rest of a valid semantic response.
    """
    problems = []
    segments = result.get("segments") if isinstance(result.get("segments"), list) else []
    segment_ids = [row.get("segment_id") for row in segments if isinstance(row, dict)]
    if not all(segment_ids) or len(segment_ids) != len(set(segment_ids)):
        problems.append("语义综合的细分 ID 必须非空且唯一")
    journey = result.get("journey") if isinstance(result.get("journey"), dict) else {}
    stages = journey.get("stages") if isinstance(journey.get("stages"), list) else []
    stage_ids = [row.get("stage_id") for row in stages if isinstance(row, dict)]
    if not all(stage_ids) or len(stage_ids) != len(set(stage_ids)):
        problems.append("语义综合的旅程阶段 ID 必须非空且唯一")
    return problems


def validate_synthesis_result(payload: dict, result: dict) -> list[str]:
    """Validate global synthesis coverage before a synthesis response becomes a checkpoint."""
    problems: list[str] = []
    events = payload.get("events") or []
    event_ids = [row.get("event_id") for row in events]
    if not all(event_ids) or len(event_ids) != len(set(event_ids)):
        return ["综合输入的 event_id 必须非空且唯一"]

    assignments = result.get("event_assignments") or []
    assigned_ids = [row.get("event_id") for row in assignments]
    if (len(assigned_ids) != len(event_ids) or set(assigned_ids) != set(event_ids)
            or len(assigned_ids) != len(set(assigned_ids))):
        problems.append("全局综合必须对每个事件恰好归属一次")

    tag_rows = result.get("tag_dictionary") or []
    tag_ids = [row.get("tag_id") for row in tag_rows]
    if not all(tag_ids) or len(tag_ids) != len(set(tag_ids)):
        problems.append("综合词典标签 ID 必须非空且唯一")
    tag_by_id = {row.get("tag_id"): row for row in tag_rows if row.get("tag_id")}

    expected_values = set()
    for event in events:
        for field_path in FIELD_NAMESPACES:
            expected_values.update((field_path, raw_value)
                                   for raw_value in _values_at(event, field_path))
    value_mappings = result.get("value_tag_mappings") or []
    mapping_keys = [(row.get("field_path"), row.get("raw_value")) for row in value_mappings]
    mapping_counts = Counter(mapping_keys)
    if set(mapping_keys) != expected_values or any(count != 1 for count in mapping_counts.values()):
        problems.append("全局综合必须对每个唯一非空字段值恰有一条词典映射")
    for mapping in value_mappings:
        expected_namespace = FIELD_NAMESPACES.get(mapping.get("field_path"))
        tag = tag_by_id.get(mapping.get("tag_id")) or {}
        if not expected_namespace or tag.get("namespace") != expected_namespace:
            problems.append("字段值 %s 的词典命名空间无效" % mapping.get("field_path"))
            break

    segments = result.get("segments") or []
    segment_ids = [row.get("segment_id") for row in segments]
    if not all(segment_ids) or len(segment_ids) != len(set(segment_ids)):
        problems.append("全局细分 ID 必须非空且唯一")
    stage_ids = [_text(row.get("stage_id")) for row in _dict(result.get("journey")).get("stages") or []]
    if len(stage_ids) != len(set(stage_ids)):
        problems.append("全局旅程阶段 ID 必须唯一")
    segment_ids = set(segment_ids)
    stage_ids = set(stage_ids)
    for assignment in assignments:
        if assignment.get("segment_id") not in segment_ids | {"OTHER"}:
            problems.append("事件 %s 的全局细分归属无效" % assignment.get("event_id"))
            break
        if assignment.get("journey_stage_id") not in stage_ids | {"JOURNEY_UNCLEAR"}:
            problems.append("事件 %s 的全局旅程归属无效" % assignment.get("event_id"))
            break
    return list(dict.fromkeys(problems))


def repair_synthesis_contract(payload: dict, result: dict,
                              dictionary_normalization: dict | None = None,
                              dictionary_status: str = "not_run") -> tuple[dict, dict[str, int]]:
    """Conservatively close deterministic synthesis coverage gaps without inventing semantics."""
    repaired = copy.deepcopy(result)
    summary = {
        "assignment_recoveries": 0,
        "assignment_fallbacks": 0,
        "segment_evidence_restorations": 0,
        "journey_evidence_restorations": 0,
        "segment_fallbacks": 0,
        "journey_fallbacks": 0,
        "raw_mapping_fallbacks": 0,
        "duplicate_assignments_collapsed": 0,
        "unknown_assignments_dropped": 0,
        "stale_evidence_removed": 0,
        "formal_segments_demoted": 0,
        "empty_journey_stages_removed": 0,
        "dictionary_candidates": 0,
        "dictionary_model_merge_groups": 0,
        "dictionary_model_merged_values": 0,
        "dictionary_surface_merged_values": 0,
    }

    events = payload.get("events") or []
    event_ids = [row.get("event_id") for row in events]
    event_by_id = {row.get("event_id"): row for row in events if row.get("event_id")}
    segments = repaired.get("segments") or []
    for segment in segments:
        cited_ids = segment.pop("evidence_event_ids", None)
        if cited_ids is not None:
            unique_ids = list(dict.fromkeys(cited_ids))
            segment["evidence"] = [
                {"event_id": event_id, "quote": _text(event_by_id.get(event_id, {}).get("evidence_quote"))}
                for event_id in unique_ids
                if event_id in event_by_id and _text(event_by_id[event_id].get("evidence_quote"))
            ]
            summary["stale_evidence_removed"] += len(unique_ids) - len(segment["evidence"])
    segment_ids = {row.get("segment_id") for row in segments if row.get("segment_id")}
    journey = repaired.get("journey") if isinstance(repaired.get("journey"), dict) else {}
    for stage in journey.get("stages") or []:
        cited_ids = stage.pop("evidence_event_ids", None) if isinstance(stage, dict) else None
        if cited_ids is not None:
            unique_ids = list(dict.fromkeys(cited_ids))
            stage["evidence"] = [
                {"event_id": event_id, "quote": _text(event_by_id.get(event_id, {}).get("evidence_quote"))}
                for event_id in unique_ids
                if event_id in event_by_id and _text(event_by_id[event_id].get("evidence_quote"))
            ]
            summary["stale_evidence_removed"] += len(unique_ids) - len(stage["evidence"])
    stage_ids = {
        row.get("stage_id") for row in journey.get("stages") or []
        if isinstance(row, dict) and row.get("stage_id")
    }
    event_id_set = set(event_ids)
    segment_evidence_by_event: dict[str, set[str]] = defaultdict(set)
    for segment in segments:
        segment_id = segment.get("segment_id")
        for evidence in segment.get("evidence") or []:
            event_id = evidence.get("event_id") if isinstance(evidence, dict) else None
            if event_id in event_id_set and segment_id in segment_ids:
                segment_evidence_by_event[event_id].add(segment_id)
    journey_evidence_by_event: dict[str, set[str]] = defaultdict(set)
    for stage in journey.get("stages") or []:
        stage_id = stage.get("stage_id") if isinstance(stage, dict) else None
        for evidence in (stage.get("evidence") or []) if isinstance(stage, dict) else []:
            event_id = evidence.get("event_id") if isinstance(evidence, dict) else None
            if event_id in event_id_set and stage_id in stage_ids:
                journey_evidence_by_event[event_id].add(stage_id)

    assignments_by_event: dict[str, list[dict]] = defaultdict(list)
    for assignment in repaired.get("event_assignments") or []:
        if not isinstance(assignment, dict):
            continue
        event_id = assignment.get("event_id")
        if event_id in event_id_set:
            assignments_by_event[event_id].append(assignment)
        else:
            summary["unknown_assignments_dropped"] += 1

    assignments = []
    for event_id in event_ids:
        candidates = assignments_by_event.get(event_id, [])
        selected = None
        if len(candidates) == 1:
            selected = dict(candidates[0])
        elif candidates:
            signatures = {
                (row.get("segment_id"), row.get("journey_stage_id"), row.get("confidence"), row.get("reason"))
                for row in candidates
            }
            if len(signatures) == 1:
                selected = dict(candidates[0])
                summary["duplicate_assignments_collapsed"] += len(candidates) - 1

        if selected is None:
            segment_evidence = segment_evidence_by_event.get(event_id, set())
            journey_evidence = journey_evidence_by_event.get(event_id, set())
            segment_id = next(iter(segment_evidence)) if len(segment_evidence) == 1 else "OTHER"
            stage_id = next(iter(journey_evidence)) if len(journey_evidence) == 1 else "JOURNEY_UNCLEAR"
            selected = {
                "event_id": event_id,
                "segment_id": segment_id,
                "journey_stage_id": stage_id,
                "confidence": "low",
                "reason": "模型未提供唯一可校验归属；唯一可回链的报告证据用于恢复归属，否则使用保守未归类，不作额外语义推断。",
            }
            summary["assignment_recoveries"] += 1
            used_fallback = False
            if segment_id == "OTHER":
                summary["segment_fallbacks"] += 1
                used_fallback = True
            else:
                summary["segment_evidence_restorations"] += 1
            if stage_id == "JOURNEY_UNCLEAR":
                summary["journey_fallbacks"] += 1
                used_fallback = True
            else:
                summary["journey_evidence_restorations"] += 1
            if used_fallback:
                summary["assignment_fallbacks"] += 1
            assignments.append(selected)
            continue

        changed = []
        if selected.get("segment_id") not in segment_ids | {"OTHER"}:
            candidates = segment_evidence_by_event.get(event_id, set())
            if len(candidates) == 1:
                selected["segment_id"] = next(iter(candidates))
                summary["segment_evidence_restorations"] += 1
                changed.append("细分 ID 无效，依据唯一回链证据恢复")
            else:
                selected["segment_id"] = "OTHER"
                summary["segment_fallbacks"] += 1
                changed.append("细分 ID 无效，回退 OTHER")
        if selected.get("journey_stage_id") not in stage_ids | {"JOURNEY_UNCLEAR"}:
            candidates = journey_evidence_by_event.get(event_id, set())
            if len(candidates) == 1:
                selected["journey_stage_id"] = next(iter(candidates))
                summary["journey_evidence_restorations"] += 1
                changed.append("旅程阶段 ID 无效，依据唯一回链证据恢复")
            else:
                selected["journey_stage_id"] = "JOURNEY_UNCLEAR"
                summary["journey_fallbacks"] += 1
                changed.append("旅程阶段 ID 无效，回退 JOURNEY_UNCLEAR")
        if changed:
            selected["confidence"] = "low"
            selected["reason"] = (_text(selected.get("reason")) + "；保守结构回退：" + "；".join(changed)).strip("；")
        selected["event_id"] = event_id
        assignments.append(selected)
    repaired["event_assignments"] = assignments
    assignment_by_event = {row["event_id"]: row for row in assignments}
    sample_by_event = {row.get("event_id"): row.get("sample_id") for row in events}
    formal_review_threshold = minimum_formal_segment_reviews(
        payload.get("sample_count") or len({row.get("sample_id") for row in events if row.get("sample_id")})
    )
    for segment in segments:
        segment_id = segment.get("segment_id")
        evidence = segment.get("evidence") or []
        kept = [row for row in evidence if assignment_by_event.get(row.get("event_id"), {}).get("segment_id") == segment_id]
        summary["stale_evidence_removed"] += len(evidence) - len(kept)
        segment["evidence"] = kept
        cited_samples = {sample_by_event.get(row.get("event_id")) for row in kept
                         if sample_by_event.get(row.get("event_id"))}
        member_samples = {sample_by_event.get(event_id) for event_id, assignment in assignment_by_event.items()
                          if assignment.get("segment_id") == segment_id and sample_by_event.get(event_id)}
        if segment.get("status") == "formal" and (
            len(member_samples) < formal_review_threshold
            or len(cited_samples) < MIN_FORMAL_SEGMENT_EVIDENCE_REVIEWS
        ):
            segment["status"] = "exploratory"
            summary["formal_segments_demoted"] += 1

    remaining_stages = []
    removed_stage_ids = set()
    for stage in journey.get("stages") or []:
        stage_id = stage.get("stage_id")
        evidence = stage.get("evidence") or []
        kept = [row for row in evidence
                if assignment_by_event.get(row.get("event_id"), {}).get("journey_stage_id") == stage_id]
        summary["stale_evidence_removed"] += len(evidence) - len(kept)
        if not kept:
            removed_stage_ids.add(stage_id)
            summary["empty_journey_stages_removed"] += 1
            continue
        stage["evidence"] = kept
        remaining_stages.append(stage)
    if removed_stage_ids:
        for assignment in assignments:
            if assignment.get("journey_stage_id") in removed_stage_ids:
                assignment["journey_stage_id"] = "JOURNEY_UNCLEAR"
                assignment["confidence"] = "low"
                assignment["reason"] = (_text(assignment.get("reason"))
                                         + "；原旅程阶段没有可回链证据，回退 JOURNEY_UNCLEAR").strip("；")
                summary["journey_fallbacks"] += 1
                summary["assignment_fallbacks"] += 1
        journey["stages"] = remaining_stages
        repaired["journey"] = journey

    tag_by_id: dict[str, dict] = {}
    ambiguous_tag_ids = set()
    for tag in repaired.get("tag_dictionary") or []:
        if not isinstance(tag, dict) or not tag.get("tag_id"):
            continue
        tag_id = tag["tag_id"]
        if tag_id in tag_by_id:
            if tag_by_id[tag_id] != tag:
                ambiguous_tag_ids.add(tag_id)
        else:
            tag_by_id[tag_id] = dict(tag)
    for tag_id in ambiguous_tag_ids:
        tag_by_id.pop(tag_id, None)

    expected_values = set()
    for event in events:
        for field_path in FIELD_NAMESPACES:
            expected_values.update((field_path, raw_value) for raw_value in _values_at(event, field_path))

    mappings_by_value: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for mapping in repaired.get("value_tag_mappings") or []:
        if not isinstance(mapping, dict):
            continue
        key = (mapping.get("field_path"), mapping.get("raw_value"))
        if key in expected_values:
            mappings_by_value[key].append(mapping)

    sample_ids_by_value: dict[tuple[str, str], set[str]] = defaultdict(set)
    event_ids_by_value: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                key = (field_path, raw_value)
                if event.get("sample_id"):
                    sample_ids_by_value[key].add(event["sample_id"])
                if event.get("event_id"):
                    event_ids_by_value[key].add(event["event_id"])

    normalization = dictionary_normalization if isinstance(dictionary_normalization, dict) else {}
    normalization_payload = {"candidates": normalization.get("candidates") or []}
    normalization_result = normalization.get("result") or {"groups": []}
    if validate_dictionary_normalization(normalization_payload, normalization_result):
        normalization_result = {"groups": []}
    candidate_by_id = {row.get("candidate_id"): row for row in normalization_payload["candidates"]
                       if row.get("candidate_id")}
    normalized_tag_by_value: dict[tuple[str, str], dict] = {}
    accepted_model_groups = 0
    accepted_model_values = 0

    def make_normalized_tag(namespace: str, keys: list[tuple[str, str]], tag_id: str,
                            canonical_name: str, canonical_name_en: str,
                            definition: str, definition_en: str, method: str) -> dict:
        support_samples = set().union(*(sample_ids_by_value.get(key, set()) for key in keys))
        support_events = set().union(*(event_ids_by_value.get(key, set()) for key in keys))
        return {
            "tag_id": tag_id,
            "namespace": namespace,
            "canonical_name": canonical_name,
            "canonical_name_en": canonical_name_en,
            "definition": definition,
            "definition_en": definition_en,
            "normalization_status": "normalized",
            "normalization_method": method,
            "raw_variants": [{"field_path": path, "raw_value": value} for path, value in sorted(keys)],
            "independent_review_count": len(support_samples),
            "support_event_ids": sorted(support_events),
        }

    for group in normalization_result.get("groups") or []:
        keys = sorted({(candidate_by_id[candidate_id]["field_path"], candidate_by_id[candidate_id]["raw_value"])
                       for candidate_id in group["candidate_ids"]})
        namespace = group["namespace"]
        digest_source = "\0".join([namespace, *[path + "\0" + value for path, value in keys]])
        tag_id = "CON-" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16]
        tag = make_normalized_tag(namespace, keys, tag_id, _text(group.get("canonical_name")),
                                  _text(group.get("canonical_name_en")), _text(group.get("definition")),
                                  _text(group.get("definition_en")), "llm_candidate_merge")
        tag_by_id[tag_id] = tag
        for key in keys:
            normalized_tag_by_value[key] = tag
        accepted_model_groups += 1
        accepted_model_values += len(keys)
    summary["dictionary_model_merge_groups"] = accepted_model_groups
    summary["dictionary_model_merged_values"] = accepted_model_values

    # Only collapse exact surface variants (Unicode canonical form, case and spacing).
    # Semantic synonym decisions remain model-assisted and bounded to frequent candidates.
    surface_groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for key in sorted(expected_values):
        if key in normalized_tag_by_value:
            continue
        field_path, raw_value = key
        namespace = FIELD_NAMESPACES[field_path]
        signature = re.sub(r"\s+", " ", unicodedata.normalize("NFC", raw_value).strip()).casefold()
        if signature:
            surface_groups[(namespace, signature)].append(key)
    surface_merged_values = 0
    for (namespace, signature), keys in surface_groups.items():
        distinct_values = {value for _, value in keys}
        if len(distinct_values) < 2:
            continue
        keys = sorted(keys)
        canonical_key = min(keys, key=lambda key: (-len(sample_ids_by_value.get(key, set())), key[1].casefold(), key[0]))
        digest_source = namespace + "\0surface\0" + signature
        tag_id = "SUR-" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16]
        tag = make_normalized_tag(
            namespace, keys, tag_id, canonical_key[1], canonical_key[1],
            "仅统一 Unicode 表示、大小写和空白，不作同义语义判断。",
            "Only Unicode form, case and whitespace are standardized; no semantic synonym judgment is made.",
            "deterministic_surface",
        )
        tag["normalization_status"] = "surface_normalized"
        tag_by_id[tag_id] = tag
        for key in keys:
            normalized_tag_by_value[key] = tag
        surface_merged_values += len(keys)
    summary["dictionary_surface_merged_values"] = surface_merged_values

    def raw_tag(field_path: str, raw_value: str) -> dict:
        namespace = FIELD_NAMESPACES[field_path]
        digest = hashlib.sha256((field_path + "\0" + raw_value).encode("utf-8")).hexdigest()[:16]
        tag_id = "RAW-" + digest
        suffix = 1
        while tag_id in tag_by_id and not (
            tag_by_id[tag_id].get("namespace") == namespace
            and tag_by_id[tag_id].get("canonical_name") == raw_value
            and tag_by_id[tag_id].get("normalization_status") == "raw_unmerged"
        ):
            suffix += 1
            tag_id = "RAW-%s-%d" % (digest, suffix)
        tag = tag_by_id.get(tag_id)
        if tag is None:
            tag = {
                "tag_id": tag_id,
                "namespace": namespace,
                "canonical_name": raw_value,
                "definition": "原值保留，未确认可进行语义归一化。 / Raw value preserved; semantic normalization not confirmed.",
                "normalization_status": "raw_unmerged",
                "normalization_method": "none",
                "raw_variants": [{"field_path": field_path, "raw_value": raw_value}],
                "independent_review_count": len(sample_ids_by_value.get((field_path, raw_value), set())),
                "support_event_ids": sorted(event_ids_by_value.get((field_path, raw_value), set())),
            }
            tag_by_id[tag_id] = tag
        return tag

    value_mappings = []
    for field_path, raw_value in sorted(expected_values):
        normalized_tag = normalized_tag_by_value.get((field_path, raw_value))
        if normalized_tag:
            mapping_method = ("model_normalized" if normalized_tag.get("normalization_method") == "llm_candidate_merge"
                              else "surface_normalized")
            value_mappings.append({"field_path": field_path, "raw_value": raw_value,
                                   "tag_id": normalized_tag["tag_id"], "mapping_method": mapping_method})
            continue
        candidates = mappings_by_value.get((field_path, raw_value), [])
        valid = []
        for mapping in candidates:
            tag = tag_by_id.get(mapping.get("tag_id"))
            if tag and mapping.get("tag_id") not in ambiguous_tag_ids \
                    and tag.get("namespace") == FIELD_NAMESPACES[field_path]:
                valid.append(mapping)
        unique_valid = {(row.get("tag_id"), row.get("mapping_method")) for row in valid}
        if len(unique_valid) == 1:
            mapping_method = next(iter(unique_valid))[1]
            mapping = next(row for row in valid if row.get("mapping_method") == mapping_method)
            value_mappings.append({
                "field_path": field_path, "raw_value": raw_value,
                "tag_id": mapping["tag_id"],
                "mapping_method": mapping_method if mapping_method in {"model_normalized", "surface_normalized", "raw_passthrough"}
                                 else "raw_passthrough",
            })
            continue
        tag = raw_tag(field_path, raw_value)
        value_mappings.append({
            "field_path": field_path, "raw_value": raw_value,
            "tag_id": tag["tag_id"], "mapping_method": "raw_passthrough",
        })
        summary["raw_mapping_fallbacks"] += 1

    repaired["value_tag_mappings"] = value_mappings
    used_tag_ids = {mapping["tag_id"] for mapping in value_mappings}
    original_tag_order = [tag.get("tag_id") for tag in repaired.get("tag_dictionary") or []
                          if isinstance(tag, dict) and tag.get("tag_id") in used_tag_ids]
    original_tag_order = list(dict.fromkeys(original_tag_order))
    ordered_ids = original_tag_order + sorted(used_tag_ids - set(original_tag_order))
    for mapping in value_mappings:
        mapping["normalization_method"] = mapping["mapping_method"]
    repaired["tag_dictionary"] = [tag_by_id[tag_id] for tag_id in ordered_ids]

    notices = []
    if summary["assignment_fallbacks"] or summary["segment_fallbacks"]:
        count = summary["segment_fallbacks"]
        notices.append({
            "zh": "有 %d 条事件的模型细分归属缺失或无效，已保守回退为 OTHER；未据此推断具体人群细分。" % count,
            "en": "%d event assignments were missing or invalid and conservatively fell back to OTHER; no specific segment was inferred." % count,
        })
    if summary["journey_fallbacks"]:
        count = summary["journey_fallbacks"]
        notices.append({
            "zh": "有 %d 条事件的旅程归属缺失或无效，已标记为 JOURNEY_UNCLEAR；不代表该阶段不存在。" % count,
            "en": "%d event journey assignments were missing or invalid and marked JOURNEY_UNCLEAR; this does not imply the stage does not exist." % count,
        })
    evidence_recoveries = (summary["segment_evidence_restorations"]
                           + summary["journey_evidence_restorations"])
    if evidence_recoveries:
        notices.append({
            "zh": "有 %d 项细分/旅程归属依据唯一的既有报告引文恢复，置信度已下调；未增加新证据。" % evidence_recoveries,
            "en": "%d segment or journey assignments were restored from unique existing report citations with reduced confidence; no new evidence was added." % evidence_recoveries,
        })
    if summary["raw_mapping_fallbacks"]:
        count = summary["raw_mapping_fallbacks"]
        status_zh = {"completed": "已完成", "not_needed": "没有足量高频候选", "failed": "未完成",
                     "not_run": "未执行"}.get(dictionary_status, "未执行")
        status_en = {"completed": "completed", "not_needed": "no eligible high-frequency candidates",
                     "failed": "failed", "not_run": "not run"}.get(dictionary_status, "not run")
        notices.append({
            "zh": "仍有 %d 个字段词项未确认语义归并，已保留原值并逐项回链；高频候选归并%s。未归一词项不等于独立需求概念。" % (
                count, status_zh),
            "en": "%d field terms did not receive a confirmed semantic merge and remain individually traceable; high-frequency candidate normalization %s. Raw terms do not necessarily represent distinct needs." % (
                count, status_en),
        })
    summary["dictionary_candidates"] = len(normalization_payload["candidates"])
    candidate_coverage = float(normalization.get("candidate_coverage") or 0.0)
    repaired["dictionary_summary"] = {
        "status": dictionary_status,
        "candidate_count": summary["dictionary_candidates"],
        "candidate_coverage": candidate_coverage,
        "model_merge_groups": summary["dictionary_model_merge_groups"],
        "model_merged_values": summary["dictionary_model_merged_values"],
        "surface_merged_values": summary["dictionary_surface_merged_values"],
        "raw_unmerged_values": summary["raw_mapping_fallbacks"],
    }
    if dictionary_status == "failed":
        notices.append({
            "zh": "高频词项归并请求未完成；本次没有丢弃或猜测词项，相关原值保持未归一状态。",
            "en": "The high-frequency term normalization request did not complete; no terms were discarded or guessed, and affected raw values remain unnormalized.",
        })
    if summary["stale_evidence_removed"] or summary["formal_segments_demoted"] \
            or summary["empty_journey_stages_removed"]:
        notices.append({
            "zh": "为保持证据与归属一致，移除了 %d 条不匹配的证据引用；%d 个证据不足的正式细分降为探索细分，移除了 %d 个无可回链引文的旅程阶段。" % (
                summary["stale_evidence_removed"], summary["formal_segments_demoted"],
                summary["empty_journey_stages_removed"]),
            "en": "%d mismatched evidence links were removed; %d formal segments with insufficient citations were demoted to exploratory, and %d journey stages without traceable citations were removed." % (
                summary["stale_evidence_removed"], summary["formal_segments_demoted"],
                summary["empty_journey_stages_removed"]),
        })
    if notices:
        repaired["limitations"] = list(repaired.get("limitations") or []) + notices
    return repaired, summary


def assemble_full_result(payload: dict, extraction: dict, synthesis: dict) -> dict:
    """Join chunk extraction with one global semantic synthesis and derive repetitive rows locally."""
    result = copy.deepcopy(synthesis)
    events = copy.deepcopy(extraction.get("events") or [])
    result.pop("value_tag_mappings", None)
    result["events"] = events
    result["review_processing"] = copy.deepcopy(extraction.get("review_processing") or [])

    values_to_tag = {
        (row.get("field_path"), row.get("raw_value")): row
        for row in synthesis.get("value_tag_mappings") or []
    }
    event_tag_mappings = []
    for event in events:
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                mapping = values_to_tag.get((field_path, raw_value))
                if mapping:
                    event_tag_mappings.append({
                        "event_id": event.get("event_id"), "field_path": field_path,
                        "raw_value": raw_value, "tag_id": mapping.get("tag_id"),
                        "mapping_method": mapping.get("mapping_method", "model_normalized"),
                    })
    result["event_tag_mappings"] = event_tag_mappings

    assignment_by_id = {row.get("event_id"): row for row in synthesis.get("event_assignments") or []}
    primary_by_sample = {
        row.get("sample_id"): row for row in events if row.get("primary_for_review") is True
    }
    review_assignments = []
    for processing in result["review_processing"]:
        sample_id = processing.get("sample_id")
        primary_event = primary_by_sample.get(sample_id)
        if primary_event:
            assignment = assignment_by_id.get(primary_event.get("event_id"), {})
            review_assignments.append({
                "sample_id": sample_id, "status": "processed_with_events",
                "primary_event_id": primary_event.get("event_id"),
                "primary_segment_id": assignment.get("segment_id", "OTHER"),
                "journey_stage_id": assignment.get("journey_stage_id", "JOURNEY_UNCLEAR"),
            })
        else:
            review_assignments.append({
                "sample_id": sample_id, "status": "no_need_signal", "primary_event_id": "",
                "primary_segment_id": "OTHER", "journey_stage_id": "JOURNEY_UNCLEAR",
            })
    result["review_assignments"] = review_assignments
    return normalize_v2_result(result)


def validate_result(payload: dict, result: dict) -> list[str]:
    """Deterministic contract checks before a V2 response can be reused or reported."""
    problems: list[str] = []
    reviews = payload.get("reviews") or []
    sample_by_id = {row.get("sample_id"): row for row in reviews if row.get("sample_id")}
    sample_ids = set(sample_by_id)
    if not sample_ids:
        return ["输入中没有可分析的评论"]
    processing = result.get("review_processing") or []
    processing_ids = [row.get("sample_id") for row in processing]
    if len(processing_ids) != len(sample_ids) or set(processing_ids) != sample_ids or len(set(processing_ids)) != len(processing_ids):
        problems.append("review_processing 必须对每条输入评论恰好覆盖一次")

    events = result.get("events") or []
    event_ids = [row.get("event_id") for row in events]
    if not all(event_ids) or len(event_ids) != len(set(event_ids)):
        problems.append("事件 ID 必须非空且唯一")
    event_by_id = {row.get("event_id"): row for row in events if row.get("event_id")}
    event_counts = Counter(row.get("sample_id") for row in events)
    primary_counts = Counter(row.get("sample_id") for row in events if row.get("primary_for_review") is True)
    review_by_id = {row.get("sample_id"): row for row in processing if row.get("sample_id")}
    for sample_id in sample_ids:
        row = review_by_id.get(sample_id) or {}
        status = row.get("status")
        if status not in ("processed_with_events", "no_need_signal"):
            problems.append("评论 %s 的处理状态无效" % sample_id)
        if int(row.get("event_count") or 0) != event_counts.get(sample_id, 0):
            problems.append("评论 %s 的事件数与事件表不一致" % sample_id)
        if (status == "processed_with_events") != bool(event_counts.get(sample_id)):
            problems.append("评论 %s 的状态与事件覆盖不一致" % sample_id)
        if (event_counts.get(sample_id, 0) > 0 and primary_counts.get(sample_id, 0) != 1) or (
                event_counts.get(sample_id, 0) == 0 and primary_counts.get(sample_id, 0) != 0):
            problems.append("评论 %s 的主事件标记数量不正确" % sample_id)

    for event in events:
        sample_id = event.get("sample_id")
        source = sample_by_id.get(sample_id)
        if source is None:
            problems.append("事件 %s 引用了未知评论" % event.get("event_id"))
            continue
        quote = _text(event.get("evidence_quote"))
        searchable = " ".join((_text(source.get("title")), _text(source.get("text"))))
        if not _quote_matches_source(quote, searchable):
            problems.append("事件 %s 的引文无法在来源评论中定位" % event.get("event_id"))
        if len(quote) > 160:
            problems.append("事件 %s 的引文超过 160 字符" % event.get("event_id"))
        motivation = _dict(event.get("motivation"))
        if _text(motivation.get("inferred")) and not _text(motivation.get("inference_basis")):
            problems.append("事件 %s 的推断动机缺少原文依据" % event.get("event_id"))

    tags = result.get("tag_dictionary") or []
    tag_ids = [row.get("tag_id") for row in tags]
    if not all(tag_ids) or len(tag_ids) != len(set(tag_ids)):
        problems.append("词典标签 ID 必须非空且唯一")
    tag_by_id = {row.get("tag_id"): row for row in tags if row.get("tag_id")}
    for tag in tags:
        if tag.get("normalization_status", "normalized") not in {
            "normalized", "surface_normalized", "raw_unmerged"
        }:
            problems.append("词典标签 %s 的归一状态无效" % tag.get("tag_id"))
            break
    expected = set()
    for event in events:
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                expected.add((event.get("event_id"), field_path, raw_value))
    mappings = result.get("event_tag_mappings") or []
    mapping_keys = [(row.get("event_id"), row.get("field_path"), row.get("raw_value")) for row in mappings]
    mapping_counts = Counter(mapping_keys)
    if set(mapping_keys) != expected or any(count != 1 for count in mapping_counts.values()):
        problems.append("每个非空事件字段值必须恰有一条原值映射")
    for mapping in mappings:
        tag = tag_by_id.get(mapping.get("tag_id")) or {}
        expected_namespace = FIELD_NAMESPACES.get(mapping.get("field_path"))
        if mapping.get("event_id") not in event_by_id or tag.get("namespace") != expected_namespace:
            problems.append("映射 %s 的事件或命名空间无效" % mapping.get("event_id"))
            break
        if mapping.get("mapping_method") not in {"model_normalized", "surface_normalized", "raw_passthrough"}:
            problems.append("映射 %s 的归一方式无效" % mapping.get("event_id"))
            break
    mappings_by_tag: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for mapping in mappings:
        if mapping.get("tag_id"):
            mappings_by_tag[mapping["tag_id"]].add((mapping.get("field_path"), mapping.get("raw_value")))
    for tag in tags:
        if "raw_variants" not in tag:
            continue
        raw_variants = {
            (row.get("field_path"), row.get("raw_value"))
            for row in tag.get("raw_variants") or [] if isinstance(row, dict)
        }
        if raw_variants != mappings_by_tag.get(tag.get("tag_id"), set()):
            problems.append("词典标签 %s 的原始词项与事件映射不一致" % tag.get("tag_id"))
            break

    segments = result.get("segments") or []
    segment_ids = [row.get("segment_id") for row in segments]
    if not all(segment_ids) or len(segment_ids) != len(set(segment_ids)):
        problems.append("细分 ID 必须非空且唯一")
    segment_by_id = {row.get("segment_id"): row for row in segments if row.get("segment_id")}
    stage_ids = [_text(row.get("stage_id")) for row in (_dict(result.get("journey")).get("stages") or [])]
    if len(stage_ids) != len(set(stage_ids)):
        problems.append("旅程阶段 ID 必须唯一")
    assignments = result.get("event_assignments") or []
    assigned_ids = [row.get("event_id") for row in assignments]
    if len(assigned_ids) != len(event_ids) or set(assigned_ids) != set(event_ids) or len(set(assigned_ids)) != len(assigned_ids):
        problems.append("每个事件必须恰有一次细分和旅程归属")
    for assignment in assignments:
        if assignment.get("segment_id") not in segment_by_id and assignment.get("segment_id") != "OTHER":
            problems.append("事件 %s 被分配到未知细分" % assignment.get("event_id"))
            break
        if assignment.get("journey_stage_id") not in stage_ids and assignment.get("journey_stage_id") != "JOURNEY_UNCLEAR":
            problems.append("事件 %s 被分配到未知旅程阶段" % assignment.get("event_id"))
            break
    assignment_by_id = {row.get("event_id"): row for row in assignments}
    review_assignments = result.get("review_assignments") or []
    review_assignment_ids = [row.get("sample_id") for row in review_assignments]
    if (len(review_assignment_ids) != len(sample_ids) or set(review_assignment_ids) != sample_ids
            or len(set(review_assignment_ids)) != len(review_assignment_ids)):
        problems.append("review_assignments 必须对每条输入评论恰好覆盖一次")
    else:
        for review_assignment in review_assignments:
            sample_id = review_assignment.get("sample_id")
            primary_events = [row for row in events if row.get("sample_id") == sample_id
                              and row.get("primary_for_review") is True]
            if not primary_events:
                valid = (review_assignment.get("status") == "no_need_signal"
                         and not review_assignment.get("primary_event_id")
                         and review_assignment.get("primary_segment_id") == "OTHER"
                         and review_assignment.get("journey_stage_id") == "JOURNEY_UNCLEAR")
            else:
                primary = primary_events[0]
                primary_assignment = assignment_by_id.get(primary.get("event_id"), {})
                valid = (review_assignment.get("status") == "processed_with_events"
                         and review_assignment.get("primary_event_id") == primary.get("event_id")
                         and review_assignment.get("primary_segment_id") == primary_assignment.get("segment_id")
                         and review_assignment.get("journey_stage_id") == primary_assignment.get("journey_stage_id"))
            if not valid:
                problems.append("评论 %s 的互斥主归属与主事件归属不一致" % sample_id)
                break

    minimum = minimum_formal_segment_reviews(len(sample_ids))
    assignment_by_id = {row.get("event_id"): row for row in assignments}
    for segment in segments:
        sid = segment.get("segment_id")
        key = segment.get("segment_key") or {}
        if set(key) != {"audience", "scenario", "motivation", "task"}:
            problems.append("细分 %s 缺少人群、场景、动机、任务四维主键" % sid)
        bilingual_fields = ("name", "synthesis", "user", "buyer", "relationship", "physical_constraints",
                            "role_needs", "scenario", "motivation", "job", "pain", "itch", "desired_outcome",
                            "caveat", "merge_reason", "split_reason", "decision_value")
        if any(not _has_bilingual(segment.get(field)) for field in bilingual_fields):
            problems.append("细分 %s 的画像字段必须中英对照" % sid)
        if any(not _has_bilingual(key.get(dimension))
               for dimension in ("audience", "scenario", "motivation", "task")):
            problems.append("细分 %s 的四维主键必须中英对照" % sid)
        member_samples = {
            event_by_id[event_id].get("sample_id")
            for event_id, assignment in assignment_by_id.items()
            if assignment.get("segment_id") == sid and event_id in event_by_id
        }
        if segment.get("status") == "formal" and len(member_samples) < minimum:
            problems.append("正式细分 %s 未达到独立评论频数门槛" % sid)
        evidence_samples = set()
        for evidence in segment.get("evidence") or []:
            event_id = evidence.get("event_id")
            event = event_by_id.get(event_id)
            quote = _text(evidence.get("quote"))
            source = sample_by_id.get((event or {}).get("sample_id")) or {}
            searchable = " ".join((_text(source.get("title")), _text(source.get("text"))))
            if (not event or assignment_by_id.get(event_id, {}).get("segment_id") != sid
                    or not _quote_matches_source(quote, searchable)):
                problems.append("细分 %s 的证据引文未回链到该细分的来源评论" % sid)
            elif len(quote) <= 160:
                evidence_samples.add(event.get("sample_id"))
        if segment.get("status") == "formal" and len(evidence_samples) < MIN_FORMAL_SEGMENT_EVIDENCE_REVIEWS:
            problems.append("正式细分 %s 缺少至少三条独立评论引文" % sid)

    for stage in _dict(result.get("journey")).get("stages") or []:
        stage_id = stage.get("stage_id")
        for field in ("name", "synthesis", "user_need", "friction_or_outcome"):
            if not _has_bilingual(stage.get(field)):
                problems.append("旅程阶段 %s 的字段 %s 必须中英对照" % (stage_id, field))
        evidence_rows = stage.get("evidence") or []
        if not evidence_rows:
            problems.append("旅程阶段 %s 缺少证据引文" % stage_id)
        for evidence in evidence_rows:
            event_id = evidence.get("event_id")
            event = event_by_id.get(event_id)
            source = sample_by_id.get((event or {}).get("sample_id")) or {}
            quote = _text(evidence.get("quote"))
            searchable = " ".join((_text(source.get("title")), _text(source.get("text"))))
            if (not event or assignment_by_id.get(event_id, {}).get("journey_stage_id") != stage_id
                    or not _quote_matches_source(quote, searchable) or len(quote) > 160):
                problems.append("旅程阶段 %s 的证据引文无法回链" % stage_id)

    for field in ("overview", "method_note"):
        if not _has_bilingual(result.get(field)):
            problems.append("%s 必须中英对照" % field)
    for field in ("segment_comparison", "cross_cutting", "exploratory_signals", "limitations"):
        for index, row in enumerate(result.get(field) or []):
            if not _has_bilingual(row):
                problems.append("%s[%s] 必须中英对照" % (field, index))
                break
    return list(dict.fromkeys(problems))


def _count_values(events: list[dict], path: str) -> list[dict]:
    counts: dict[str, set[str]] = defaultdict(set)
    for event in events:
        value: Any = event
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = _text(item)
            if text:
                counts[text].add(_text(event.get("review_id")))
    return [{"label": label, "review_count": len(review_ids)}
            for label, review_ids in sorted(counts.items(), key=lambda item: (-len(item[1]), item[0]))]


def _report_aggregate_checks(events: list[dict], assignments: list[dict], segment_records: list[dict],
                            journey: dict, review_assignments: list[dict], total_reviews: int) -> list[dict]:
    """Check report memberships and counts against the canonical event assignments."""
    event_ids = [row.get("event_id") for row in events]
    event_by_id = {row.get("event_id"): row for row in events if row.get("event_id")}
    assignment_ids = [row.get("event_id") for row in assignments]
    assignment_by_id = {row.get("event_id"): row for row in assignments if row.get("event_id")}
    segment_by_id = {row.get("segment_id"): row for row in segment_records if row.get("segment_id")}
    stage_rows = journey.get("stages") or []
    stage_ids = {row.get("stage_id") for row in stage_rows if row.get("stage_id")}
    defined_segment_ids = set(segment_by_id) - {"OTHER"}
    defined_stage_ids = stage_ids - {"JOURNEY_UNCLEAR"}

    assignment_valid = (
        len(assignment_ids) == len(event_ids)
        and len(set(assignment_ids)) == len(assignment_ids)
        and set(assignment_ids) == set(event_ids)
        and all(
            row.get("segment_id") in defined_segment_ids | {"OTHER"}
            and row.get("journey_stage_id") in defined_stage_ids | {"JOURNEY_UNCLEAR"}
            for row in assignments
        )
    )

    expected_members: dict[str, list[str]] = defaultdict(list)
    event_projection_matches = True
    for event in events:
        event_id = event.get("event_id")
        assignment = assignment_by_id.get(event_id)
        if assignment is None:
            event_projection_matches = False
            continue
        if (event.get("segment_id") != assignment.get("segment_id")
                or event.get("journey_stage_id") != assignment.get("journey_stage_id")):
            event_projection_matches = False
        segment_id = assignment.get("segment_id")
        if segment_id in defined_segment_ids:
            expected_members[segment_id].append(event_id)

    membership_matches = assignment_valid and event_projection_matches
    metrics_match = assignment_valid
    for segment_id, segment in segment_by_id.items():
        if segment_id == "OTHER":
            # OTHER is a fallback assignment, not a reportable synthesized segment.
            membership_matches = membership_matches and not segment.get("member_event_ids")
            continue
        expected_ids = expected_members.get(segment_id, [])
        actual_ids = segment.get("member_event_ids") or []
        if (len(actual_ids) != len(set(actual_ids))
                or set(actual_ids) != set(expected_ids)
                or len(actual_ids) != len(expected_ids)):
            membership_matches = False

        unique_events = {event_id: event_by_id[event_id] for event_id in set(expected_ids)
                         if event_id in event_by_id}
        unique_reviews = {_text(event.get("review_id")) for event in unique_events.values()
                          if _text(event.get("review_id"))}
        unique_asins = {_text(event.get("asin")) for event in unique_events.values()
                        if _text(event.get("asin"))}
        primary_reviews = {_text(row.get("sample_id")) for row in review_assignments
                           if row.get("primary_segment_id") == segment_id and _text(row.get("sample_id"))}
        expected_metrics = {
            "review_count": len(unique_reviews),
            "review_coverage": round(len(unique_reviews) / total_reviews, 6) if total_reviews else 0,
            "event_count": len(unique_events),
            "asin_count": len(unique_asins),
            "primary_review_count": len(primary_reviews),
        }
        actual_metrics = segment.get("metrics") if isinstance(segment.get("metrics"), dict) else {}
        if any(actual_metrics.get(key) != value for key, value in expected_metrics.items()):
            metrics_match = False

    cells = journey.get("cells") or []
    cell_rows_by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for cell in cells:
        cell_rows_by_pair[(cell.get("segment_id"), cell.get("stage_id"))].append(cell)
    expected_pairs = {(segment_id, stage_id) for segment_id in defined_segment_ids
                      for stage_id in defined_stage_ids}
    actual_pairs = set(cell_rows_by_pair)
    cells_match = actual_pairs == expected_pairs and all(
        len(cell_rows_by_pair[pair]) == 1 for pair in cell_rows_by_pair
    )
    for segment_id, stage_id in expected_pairs:
        pair_cells = cell_rows_by_pair.get((segment_id, stage_id), [])
        if len(pair_cells) != 1:
            cells_match = False
            continue
        cell = pair_cells[0]
        member_ids = set(segment_by_id.get(segment_id, {}).get("member_event_ids") or [])
        membership_stage_ids = {
            event_id for event_id in member_ids
            if assignment_by_id.get(event_id, {}).get("journey_stage_id") == stage_id
        }
        assignment_stage_ids = {
            event_id for event_id, assignment in assignment_by_id.items()
            if assignment.get("segment_id") == segment_id and assignment.get("journey_stage_id") == stage_id
        }
        matching_ids = assignment_stage_ids
        matching_events = [event_by_id[event_id] for event_id in matching_ids if event_id in event_by_id]
        review_count = len({_text(event.get("review_id")) for event in matching_events
                            if _text(event.get("review_id"))})
        expected_cell = {
            "status": "observed" if matching_ids else "not_observed",
            "event_count": len(matching_ids),
            "review_count": review_count,
            "actions": _count_values(matching_events, "scenario.context")[:5],
            "goals": _count_values(matching_events, "task.desired_outcome")[:5],
            "pains": _count_values(matching_events, "experience.pain")[:5],
            "itches": _count_values(matching_events, "experience.itch")[:5],
            "workarounds": [],
        }
        if membership_stage_ids != assignment_stage_ids or any(
                cell.get(key) != value for key, value in expected_cell.items()):
            cells_match = False

    expected_stage_assignments = Counter(
        (row.get("event_id"), row.get("journey_stage_id")) for row in assignments
    )
    actual_stage_assignments = [
        (row.get("event_id"), row.get("stage_id")) for row in journey.get("event_assignments") or []
    ]
    journey_assignments_match = (
        len(actual_stage_assignments) == len(expected_stage_assignments)
        and Counter(actual_stage_assignments) == expected_stage_assignments
        and journey.get("unclear_event_count") == sum(
            1 for row in assignments if row.get("journey_stage_id") == "JOURNEY_UNCLEAR"
        )
    )
    return [
        {"name": "事件归属覆盖唯一且指向已定义细分/阶段（OTHER 与 JOURNEY_UNCLEAR 为显式兜底）",
         "pass": assignment_valid},
        {"name": "事件投影与细分成员逐条一致（OTHER 不进入细分成员）",
         "pass": membership_matches},
        {"name": "细分 review/event/ASIN 指标等于唯一成员计数",
         "pass": metrics_match},
        {"name": "旅程单元格与成员、阶段归属及唯一计数一致",
         "pass": cells_match},
        {"name": "旅程归属投影与 JOURNEY_UNCLEAR 计数一致",
         "pass": journey_assignments_match},
    ]


def _segment_profile(segment: dict) -> dict:
    return {
        "users": _pair(segment.get("user")),
        "buyers": _pair(segment.get("buyer")),
        "relationships": _pair(segment.get("relationship")),
        "physical_constraints": _pair(segment.get("physical_constraints")),
        "role_needs": _pair(segment.get("role_needs")),
        "scenario": _pair(segment.get("scenario")),
        "motivation": _pair(segment.get("motivation")),
        "task": _pair(segment.get("job")),
        "pains": _pair(segment.get("pain")),
        "itches": _pair(segment.get("itch")),
        "desired_outcomes": _pair(segment.get("desired_outcome")),
    }


def adapt_result(master: dict, cleaned_rows: list[dict], result: dict, model_ref: str,
                 prompt_version: str = PROMPT_VERSION) -> dict:
    """Project the V2 result into the existing six-module / twelve-sheet report contract."""
    payload = build_payload(master, cleaned_rows)
    result = normalize_v2_result(result)
    errors = validate_result(payload, result)
    if errors:
        raise ValueError("V2 结构与覆盖门禁未通过：" + "；".join(errors[:12]))

    source_by_id = {row.get("review_id"): row for row in cleaned_rows}
    assignments = {row["event_id"]: row for row in result.get("event_assignments") or []}
    raw_events = result.get("events") or []
    event_by_id: dict[str, dict] = {}
    events = []
    for raw in raw_events:
        sample_id = raw.get("sample_id")
        source = source_by_id[sample_id]
        event = dict(raw)
        event["review_id"] = sample_id
        event["asin"] = _text(source.get("asin"))
        event["rating"] = source.get("rating", "")
        event["review_date"] = _text(source.get("review_date"))
        event["segment_id"] = assignments[event["event_id"]].get("segment_id")
        event["journey_stage_id"] = assignments[event["event_id"]].get("journey_stage_id")
        # Existing workbook columns use plural desired_outcomes and a normalized workaround shape.
        task = dict(_dict(event.get("task")))
        if "desired_outcomes" not in task:
            task["desired_outcomes"] = [task.get("desired_outcome")] if _text(task.get("desired_outcome")) else []
        event["task"] = task
        workaround = dict(_dict(event.get("workaround")))
        action = _text(workaround.get("action"))
        workaround.setdefault("exists", bool(action))
        workaround.setdefault("raw_action", action)
        workaround.setdefault("standardized_action", action)
        event["workaround"] = workaround
        event["fact_inference"] = _text(event.get("fact_inference_boundary"))
        event_by_id[event["event_id"]] = event
        events.append(event)

    processing_by_id = {row["sample_id"]: row for row in result.get("review_processing") or []}
    primary_by_id = {row["sample_id"]: row for row in result.get("review_assignments") or []}
    event_counts = Counter(event.get("review_id") for event in events)
    processing = []
    for source in cleaned_rows:
        rid = source.get("review_id")
        source_row = processing_by_id[rid]
        has_events = bool(event_counts.get(rid))
        processing.append({
            "review_id": rid,
            "status": "已生成事件" if has_events else "无可用需求信号",
            "reason": _text(source_row.get("reason")),
            "event_count": event_counts.get(rid, 0),
            "primary_event_id": _text(primary_by_id[rid].get("primary_event_id")),
            "primary_segment_id": _text(primary_by_id[rid].get("primary_segment_id")),
            "journey_stage_id": _text(primary_by_id[rid].get("journey_stage_id")),
        })

    tags = []
    for raw in result.get("tag_dictionary") or []:
        tag = dict(raw)
        tag.setdefault("canonical_name_en", "")
        tag.setdefault("definition", "")
        tag.setdefault("definition_en", "")
        tag.setdefault("normalization_status", "normalized")
        tag.setdefault("support_event_ids", [])
        tag.setdefault("independent_review_count", 0)
        tags.append(tag)
    mappings = []
    for raw in result.get("event_tag_mappings") or []:
        mapping = dict(raw)
        mapping.setdefault("mapping_method", "raw_passthrough" if mapping.get("tag_id", "").startswith("RAW-") else "model_normalized")
        mappings.append(mapping)

    segment_records = []
    assigned_by_segment: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        assigned_by_segment[event.get("segment_id")].append(event)
    for raw in result.get("segments") or []:
        sid = raw["segment_id"]
        members = assigned_by_segment.get(sid, [])
        member_event_ids = [event["event_id"] for event in members]
        evidence = raw.get("evidence") or []
        evidence_snippets = []
        for item in evidence:
            event = event_by_id.get(item.get("event_id"))
            if event:
                evidence_snippets.append({"event_id": event["event_id"], "quote": _text(item.get("quote") or event.get("evidence_quote")),
                                          "quote_omitted": False})
        supporting = [item for item in raw.get("supporting_event_ids") or [] if item in event_by_id]
        evidence_ids = [item["event_id"] for item in evidence_snippets] or supporting[:20] or member_event_ids[:20]
        key = raw.get("segment_key") or {}
        key_zh = {dimension: _pair(key.get(dimension)).get("zh") for dimension in ("audience", "scenario", "motivation", "task")}
        key_en = {dimension: _pair(key.get(dimension)).get("en") for dimension in ("audience", "scenario", "motivation", "task")}
        profile = _segment_profile(raw)
        name = _pair(raw.get("name"))
        synthesis = _pair(raw.get("synthesis"))
        job = _pair(raw.get("job"))
        review_primary_count = sum(1 for row in result.get("review_assignments") or []
                                   if row.get("primary_segment_id") == sid)
        segment_records.append({
            "segment_id": sid,
            "status": "formal" if raw.get("status") == "formal" else "exploratory",
            "name": name["zh"], "name_en": name["en"],
            "segment_key": key_zh, "segment_key_en": key_en,
            "persona_summary": synthesis["zh"], "persona_summary_en": synthesis["en"],
            "jtbd_statement": job["zh"], "jtbd_statement_en": job["en"],
            "merge_reason": _pair(raw.get("merge_reason"))["zh"],
            "merge_reason_en": _pair(raw.get("merge_reason"))["en"],
            "split_reason": _pair(raw.get("split_reason"))["zh"],
            "split_reason_en": _pair(raw.get("split_reason"))["en"],
            "decision_value": _pair(raw.get("decision_value")),
            "caveat": _pair(raw.get("caveat")),
            "profile_synthesis": profile,
            "member_event_ids": member_event_ids,
            "evidence_event_ids": evidence_ids,
            "evidence_snippets": evidence_snippets,
            "metrics": {
                "review_count": len({event.get("review_id") for event in members}),
                "review_coverage": round(len({event.get("review_id") for event in members}) / len(cleaned_rows), 6) if cleaned_rows else 0,
                "event_count": len(members),
                "asin_count": len({event.get("asin") for event in members if event.get("asin")}),
                "primary_review_count": review_primary_count,
            },
            "pains": _count_values(members, "experience.pain"),
            "itches": _count_values(members, "experience.itch"),
            "desired_outcomes": _count_values(members, "task.desired_outcome"),
            "audience_details": {
                "users": _count_values(members, "audience.user"),
                "buyers": [], "relationships": [], "physical_constraints": [], "skill_levels": [],
                "role_needs": [],
            },
            "workarounds": [], "need_priorities": [],
        })

    journey_raw = _dict(result.get("journey"))
    stages = []
    for index, raw in enumerate(journey_raw.get("stages") or [], 1):
        name = _pair(raw.get("name"))
        synthesis = _pair(raw.get("synthesis"))
        need = _pair(raw.get("user_need"))
        friction = _pair(raw.get("friction_or_outcome"))
        stages.append({
            "stage_id": raw.get("stage_id"), "order": index,
            "name": name["zh"], "name_en": name["en"],
            "definition": synthesis["zh"], "definition_en": synthesis["en"],
            "user_need": need, "friction_or_outcome": friction,
            "confidence": _text(raw.get("confidence")),
            "evidence": raw.get("evidence") or [],
        })
    stage_assignments = [{"event_id": row["event_id"], "stage_id": row.get("journey_stage_id")}
                         for row in result.get("event_assignments") or []]
    cells = []
    stage_id_set = {stage.get("stage_id") for stage in stages}
    for segment in segment_records:
        for stage in stages:
            matching = [event_by_id[eid] for eid in segment.get("member_event_ids") or []
                        if eid in event_by_id and assignments[eid].get("journey_stage_id") == stage.get("stage_id")]
            cells.append({
                "segment_id": segment["segment_id"], "stage_id": stage["stage_id"],
                "status": "observed" if matching else "not_observed",
                "review_count": len({event.get("review_id") for event in matching}),
                "event_count": len(matching),
                "actions": _count_values(matching, "scenario.context")[:5],
                "goals": _count_values(matching, "task.desired_outcome")[:5],
                "pains": _count_values(matching, "experience.pain")[:5],
                "itches": _count_values(matching, "experience.itch")[:5],
                "workarounds": [],
            })
    journey = {
        "schema": "amazon-voc-dynamic-journey/v3",
        "interpretation_type": _text(journey_raw.get("interpretation_type")),
        "summary": _pair(journey_raw.get("summary")),
        "limitations": _pair(journey_raw.get("limitations")),
        "stages": stages, "cells": cells, "event_assignments": stage_assignments,
        "unclear_event_count": sum(1 for row in result.get("event_assignments") or []
                                    if row.get("journey_stage_id") not in stage_id_set),
    }

    evidence_index = {}
    for event in events:
        source = source_by_id.get(event["review_id"], {})
        evidence_index[event["event_id"]] = {
            "review_id": event["review_id"], "asin": event.get("asin"), "rating": event.get("rating"),
            "review_date": event.get("review_date"), "evidence_quote": event.get("evidence_quote"),
            "review_text": _text(source.get("content")),
        }
    total_reviews = len(cleaned_rows)
    assigned_samples = {
        event_by_id[row["event_id"]].get("review_id")
        for row in result.get("event_assignments") or []
        if row.get("event_id") in event_by_id and row.get("segment_id") != "OTHER"
    }
    threshold = minimum_formal_segment_reviews(total_reviews)
    processing_ids = [row.get("review_id") for row in processing]
    source_review_ids = [row.get("review_id") for row in cleaned_rows]
    source_quotes_valid = all(
        event.get("review_id") in source_by_id
        and _quote_matches_source(
            _text(event.get("evidence_quote")),
            " ".join((_text(source_by_id[event.get("review_id")].get("review_title")),
                      _text(source_by_id[event.get("review_id")].get("content")))),
        )
        for event in events
    )
    expected_mapping_keys = set()
    for event in events:
        for field_path in FIELD_NAMESPACES:
            for raw_value in _values_at(event, field_path):
                expected_mapping_keys.add((event.get("event_id"), field_path, raw_value))
    actual_mapping_keys = [
        (row.get("event_id"), row.get("field_path"), row.get("raw_value")) for row in mappings
    ]
    mapping_counts = Counter(actual_mapping_keys)
    mapping_coverage_valid = (
        set(actual_mapping_keys) == expected_mapping_keys
        and len(actual_mapping_keys) == len(expected_mapping_keys)
        and all(count == 1 for count in mapping_counts.values())
    )
    aggregate_checks = _report_aggregate_checks(
        events, result.get("event_assignments") or [], segment_records, journey,
        result.get("review_assignments") or [], total_reviews,
    )
    assignment_check = next(check for check in aggregate_checks
                            if check["name"].startswith("事件归属覆盖唯一"))
    formal_threshold_valid = all(
        segment.get("status") != "formal"
        or segment.get("metrics", {}).get("review_count", 0) >= threshold
        for segment in segment_records
    )
    checks = [
        {"name": "all uploaded reviews covered once",
         "pass": len(processing_ids) == total_reviews and len(set(processing_ids)) == len(processing_ids)
         and set(processing_ids) == set(source_review_ids)},
        {"name": "every event has a source review and locatable quote", "pass": source_quotes_valid},
        {"name": "all required event values mapped exactly once", "pass": mapping_coverage_valid},
        {"name": "every event has one valid segment and journey assignment",
         "pass": assignment_check["pass"]},
        {"name": "formal segment review threshold", "pass": formal_threshold_valid, "detail": threshold},
    ]
    checks.extend(aggregate_checks)
    automatic_gate_pass = all(check.get("pass") is True for check in checks)
    automatic_gate_failed_count = sum(1 for check in checks if check.get("pass") is not True)
    quality = {
        "schema": "amazon-voc-quality/v2",
        "all_automatic_gates_green": automatic_gate_pass,
        "acceptance_ready": False,
        "gates": [
            {"gate_id": "V2-AUTO", "name": "结构、覆盖、映射与输出自动门禁", "pass": automatic_gate_pass,
             "blocking": True, "failed_count": automatic_gate_failed_count, "checks": checks},
            {"gate_id": "V2-HUMAN", "name": "独立语义人工抽查", "pass": False, "blocking": True,
             "failed_count": 1, "checks": [{"name": "独立语义抽查完成", "pass": False,
                 "detail": "需按 ASIN × 评分分层抽查至少 10% 事件；当前仍待人工检查报告。"}]},
        ],
    }
    task = {
        "task_id": master.get("task_id"), "marketplace": master.get("marketplace"),
        "country": master.get("country"), "language": master.get("language"),
        "category": master.get("category"), "review_count": total_reviews,
        "review_asin_count": len({row.get("asin") for row in cleaned_rows if row.get("asin")}),
    }
    analysis = {
        "schema": "amazon-voc-analysis/v4", "analysis_mode": "full_voc_v2",
        "task": task,
        "method": {
            "segment_dimensions": ["audience", "scenario", "motivation", "task"],
            "primary_frequency_metric": "independent_review_coverage",
            "evidence_scope": "uploaded_review_workbook_only",
            "minimum_formal_segment_reviews": threshold,
            "kano_boundary": "本版本不输出 KANO 产品建议。",
            "prompt_versions": {"full_voc_v2": prompt_version},
            "models": [model_ref] if model_ref else [],
            "human_semantic_review": "pending",
        },
        "counts": {
            "reviews": total_reviews, "events": len(events), "segments": len(segment_records),
            "tags": sum(1 for tag in tags if tag.get("normalization_status") != "raw_unmerged"),
            "raw_unmerged_tags": sum(1 for tag in tags if tag.get("normalization_status") == "raw_unmerged"),
            "model_merged_values": sum(1 for row in mappings if row.get("mapping_method") == "model_normalized"),
            "surface_normalized_values": sum(1 for row in mappings if row.get("mapping_method") == "surface_normalized"),
            "normalized_value_coverage_pct": round(
                100 * sum(1 for row in mappings if row.get("mapping_method") != "raw_passthrough") / len(mappings), 1
            ) if mappings else 0.0,
            "other_events": sum(1 for row in result.get("event_assignments") or [] if row.get("segment_id") == "OTHER"),
            "assigned_reviews": len(assigned_samples),
            "primary_segment_assigned_reviews": sum(1 for row in result.get("review_assignments") or []
                                                       if row.get("primary_segment_id") != "OTHER"),
        },
        "dictionary_summary": copy.deepcopy(result.get("dictionary_summary") or {}),
        "overview": _pair(result.get("overview")),
        "method_note": _pair(result.get("method_note")),
        "segments": segment_records, "journey": journey,
        "segment_comparison": [_pair(row) for row in result.get("segment_comparison") or []],
        "cross_cutting": [_pair(row) for row in result.get("cross_cutting") or []],
        "exploratory_signals": [_pair(row) for row in result.get("exploratory_signals") or []],
        "limitations": [_pair(row) for row in result.get("limitations") or []],
        "quality": quality,
        "validation": {"schema": "amazon-voc-v2-validation", "all_green": automatic_gate_pass,
                       "human_semantic_review": "pending"},
    }

    return {
        "analysis": analysis,
        "events_doc": {"schema": "amazon-voc-events/v2", "metadata": {
            "model": model_ref, "model_version": model_ref, "prompt_version": prompt_version,
            "generated_at": ""}, "events": events},
        "processing_doc": {"schema": "amazon-voc-review-processing/v2", "reviews": processing},
        "dictionary_doc": {"schema": "amazon-voc-dictionary/v2", "metadata": {"prompt_version": prompt_version},
                           "tags": tags, "mappings": mappings},
        "segment_model": {"schema": "amazon-voc-segment-model/v2", "metadata": {"model": model_ref,
                           "prompt_version": prompt_version}, "segments": segment_records,
                           "journey": {**journey_raw, "stages": stages, "event_assignments": stage_assignments}},
        "evidence_index": evidence_index,
        "quality_report": quality,
        "validation_errors": [],
    }
