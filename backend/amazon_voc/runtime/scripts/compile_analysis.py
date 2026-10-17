# -*- coding: utf-8 -*-
"""Compile V4 model outputs into one deterministic analysis source.

Inputs written by the model workflow:
  cache/events.json
  cache/segment_model.json

Outputs used by both HTML and Excel:
  cache/analysis.json
  cache/evidence_index.json
  cache/journey_matrix.json
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from validate_methodology import validate_methodology


def _optional(workdir, name, default):
    path = common.cache_path(workdir, name)
    return common.read_json(path) if os.path.exists(path) else default


def _redact_full_review_text(payload, events):
    """Keep verbatim full reviews out of report text, even if a model copies one into a summary field."""
    normalize = lambda value: re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    review_texts = sorted({normalize(event.get("review_text")) for event in events
                           if len(normalize(event.get("review_text"))) >= 20}, key=len, reverse=True)
    omission = "完整评论原文未嵌入报告；请在 Excel 评论证据索引中回查。"

    def has_full_review(text):
        normalized = normalize(text)
        if len(normalized) < 20:
            return False
        for source in review_texts:
            if len(source) > len(normalized):
                continue
            if source in normalized:
                return True
        return False

    def visit(value):
        if isinstance(value, dict):
            redacted = 0
            quote_redacted = False
            for key, item in list(value.items()):
                value[key], count = visit(item)
                redacted += count
                if key == "quote" and count:
                    quote_redacted = True
            if quote_redacted:
                value["quote_omitted"] = True
            return value, redacted
        if isinstance(value, list):
            output = []
            redacted = 0
            for item in value:
                cleaned, count = visit(item)
                output.append(cleaned)
                redacted += count
            return output, redacted
        if isinstance(value, str) and has_full_review(value):
            return omission, 1
        return value, 0

    return visit(payload)[1]


def _workarounds(events):
    rows = []
    for event in events:
        value = event.get("workaround") or {}
        if value.get("exists") in (True, "是", "yes", "明确"):
            rows.append({
                "event_id": event.get("event_id"),
                "review_id": event.get("review_id"),
                "raw_action": value.get("raw_action", ""),
                "standardized_action": value.get("standardized_action", ""),
                "strategy": value.get("strategy", ""),
                "recovery_goal": value.get("recovery_goal", ""),
                "costs": common.as_list(value.get("costs")),
                "effect": value.get("effect", ""),
            })
    return rows


def _omit_full_review_text(value, events):
    """Keep full review bodies in the evidence workbook, never in report narratives."""
    if isinstance(value, dict):
        return {key: _omit_full_review_text(item, events) for key, item in value.items()}
    if isinstance(value, list):
        return [_omit_full_review_text(item, events) for item in value]
    if not isinstance(value, str):
        return value
    for event in events:
        source = str(event.get("review_text") or "")
        if len(source.strip()) >= 20 and source in value:
            marker = "完整评论原文已省略，请按事件ID %s 回查" % event.get("event_id", "")
            value = value.replace(source, marker)
    return value


def _segment_payload(segment, events, denominator):
    member_ids = set(segment.get("member_event_ids") or [])
    members = [item for item in events if item.get("event_id") in member_ids]
    review_count = len(common.independent_review_ids(members))
    asin_count = len(common.independent_asins(members))
    payload = _omit_full_review_text(
        {key: value for key, value in segment.items()
         if key not in {"external_evidence", "external_research_eligible", "recommendations"}},
        events,
    )
    payload["metrics"] = {
        "review_count": review_count,
        "review_coverage": common.review_coverage(members, denominator),
        "event_count": len(members),
        "asin_count": asin_count,
        "negative_review_count": len({item.get("review_id") for item in members
                                      if common.nested(item, "experience", "sentiment") in ("负面", "混合", "negative", "mixed")}),
        "workaround_review_count": len({row.get("review_id") for row in _workarounds(members)}),
    }
    payload["pains"] = common.count_values(members, ("experience", "pain"))
    payload["itches"] = common.count_values(members, ("experience", "itch"))
    payload["desired_outcomes"] = common.count_values(members, ("task", "desired_outcomes"))
    payload["workarounds"] = _workarounds(members)
    payload["audience_details"] = {
        "users": common.count_values(members, ("audience", "user")),
        "buyers": common.count_values(members, ("audience", "buyer")),
        "relationships": common.count_values(members, ("audience", "relationship")),
        "physical_constraints": common.count_values(members, ("audience", "physical_constraints")),
        "skill_levels": common.count_values(members, ("audience", "skill_level")),
        "role_needs": common.count_values(members, ("audience", "role_needs")),
    }
    payload["evidence_event_ids"] = common.unique(segment.get("evidence_event_ids") or segment.get("member_event_ids") or [])[:20]
    evidence_ids = set(payload["evidence_event_ids"])
    payload["evidence_snippets"] = []
    for item in members:
        if item.get("event_id") not in evidence_ids:
            continue
        quote = str(item.get("evidence_quote") or "")
        source = str(item.get("review_text") or "")
        normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
        quote_normalized = normalize(quote)
        source_normalized = normalize(source)
        quote_omitted = bool((source_normalized and source_normalized in quote_normalized) or
                             (quote_normalized and len(quote_normalized) < 20))
        payload["evidence_snippets"].append({
            "event_id": item.get("event_id"),
            "quote": "" if quote_omitted else quote,
            "quote_omitted": quote_omitted,
        })
    return payload


def _journey(model, segments, events):
    journey = model.get("journey") or {}
    assignments = journey.get("event_assignments") or []
    stage_by_event = {item.get("event_id"): item.get("stage_id") for item in assignments}
    event_by_id = {item.get("event_id"): item for item in events}
    stages = sorted(journey.get("stages") or [], key=lambda item: (item.get("order", 999), item.get("name", "")))
    cells = []
    for segment in segments:
        for stage in stages:
            stage_events = [event_by_id[event_id] for event_id in segment.get("member_event_ids") or []
                            if event_id in event_by_id and stage_by_event.get(event_id) == stage.get("stage_id")]
            cells.append({
                "segment_id": segment.get("segment_id"),
                "stage_id": stage.get("stage_id"),
                "status": "observed" if stage_events else "not_observed",
                "review_count": len(common.independent_review_ids(stage_events)),
                "event_count": len(stage_events),
                "actions": common.count_values(stage_events, ("scenario", "journey_action"))[:5],
                "goals": common.count_values(stage_events, ("task", "desired_outcomes"))[:5],
                "pains": common.count_values(stage_events, ("experience", "pain"))[:5],
                "itches": common.count_values(stage_events, ("experience", "itch"))[:5],
                "workarounds": _workarounds(stage_events)[:5],
            })
    return {"schema": "amazon-voc-dynamic-journey/v3", "stages": stages, "cells": cells}


def compile_analysis(workdir):
    validation = validate_methodology(workdir)
    if not validation.get("all_green"):
        raise ValueError("方法门禁未通过，不能编译报告")

    master = common.read_json(common.cache_path(workdir, "master.json"))
    cleaned = common.read_json(common.cache_path(workdir, "cleaned_reviews.json"))
    events_doc = common.read_json(common.cache_path(workdir, "events.json"))
    model = common.read_json(common.cache_path(workdir, "segment_model.json"))
    events = events_doc.get("events") or []
    denominator = len(cleaned.get("rows") or [])

    segments = [_segment_payload(item, events, denominator)
                for item in model.get("segments") or []]

    journey_doc = _journey(model, segments, events)
    evidence_index = {}
    for event in events:
        evidence_index[event.get("event_id")] = {
            "review_id": event.get("review_id"), "asin": event.get("asin"),
            "rating": event.get("rating", ""), "review_date": event.get("review_date", ""),
            "evidence_quote": event.get("evidence_quote", ""), "review_text": event.get("review_text", ""),
        }

    quality = _optional(workdir, "quality_report.json", {})
    dictionary = _optional(workdir, "tag_dictionary.json", {"tags": [], "mappings": []})
    tags = dictionary.get("tags") or []
    normalized_tag_count = sum(tag.get("normalization_status") != "raw_unmerged" for tag in tags)
    raw_unmerged_tag_count = sum(tag.get("normalization_status") == "raw_unmerged" for tag in tags)
    output = {
        "schema": "amazon-voc-analysis/v4",
        "task": {
            "task_id": master.get("task_id"), "marketplace": master.get("marketplace"),
            "country": master.get("country"), "language": master.get("language"),
            "category": master.get("category"), "review_count": denominator,
            "review_asin_count": (master.get("stats") or {}).get("review_asin_count", 0),
        },
        "method": {
            "segment_dimensions": ["audience", "scenario", "motivation", "task"],
            "primary_frequency_metric": "independent_review_coverage",
            "evidence_scope": "uploaded_review_workbook_only",
            "minimum_formal_segment_reviews": validation.get("minimum_formal_segment_reviews"),
            "kano_boundary": "类KANO推断，不是正式问卷结论",
            "prompt_versions": {
                "events": (events_doc.get("metadata") or {}).get("prompt_version", ""),
                "segments": (model.get("metadata") or {}).get("prompt_version", ""),
                "dictionary": (dictionary.get("metadata") or {}).get("prompt_version", ""),
            },
            "models": common.unique([(events_doc.get("metadata") or {}).get("model"),
                                      (events_doc.get("metadata") or {}).get("review_model"),
                                      (model.get("metadata") or {}).get("model")]),
        },
        "counts": {"reviews": denominator, "events": len(events), "segments": len(segments),
                   "tags": normalized_tag_count, "raw_unmerged_tags": raw_unmerged_tag_count},
        "segments": segments,
        "journey": journey_doc,
        "validation": validation,
        "quality": quality,
    }
    output["method"]["full_review_text_redacted_field_count"] = _redact_full_review_text(output, events)
    common.write_json(common.cache_path(workdir, "journey_matrix.json"), journey_doc)
    common.write_json(common.cache_path(workdir, "evidence_index.json"), evidence_index)
    common.write_json(common.cache_path(workdir, "analysis.json"), output)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    output = compile_analysis(args.workdir)
    formal = len([item for item in output["segments"] if item.get("status") == "formal"])
    exploratory = len(output["segments"]) - formal
    print("[compile] 正式细分=%s 探索线索=%s 旅程阶段=%s" %
          (formal, exploratory, len(output["journey"]["stages"])))


if __name__ == "__main__":
    main()
