"""Offline validation and stable identity assignment for batched VOC V2 extraction."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping

EXTRACT_PROMPT_VERSION = "amazon-voc-full-v2.4-extract"

_STATUSES = {"processed_with_events", "no_need_signal"}
_EVENT_GROUPS = {
    "audience": ("user",),
    "scenario": ("context",),
    "motivation": ("explicit", "inferred", "inference_basis"),
    "task": ("core_job", "desired_outcome"),
    "experience": ("object", "result", "pain", "itch"),
    "workaround": ("action",),
}
_CONFIDENCE = {"high", "medium", "low"}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normalized_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _review_map(payload: Mapping[str, Any], errors: list[str]) -> dict[str, Mapping[str, Any]]:
    rows = payload.get("reviews")
    if not isinstance(rows, list):
        errors.append("payload.reviews must be an array")
        return {}
    if payload.get("sample_count") != len(rows):
        errors.append("payload.sample_count must equal len(payload.reviews)")
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            errors.append(f"payload.reviews[{index}] must be an object")
            continue
        sample_id = _text(row.get("sample_id")).strip()
        if not sample_id:
            errors.append(f"payload.reviews[{index}].sample_id is required")
        elif sample_id in by_id:
            errors.append(f"duplicate input sample_id: {sample_id}")
        else:
            by_id[sample_id] = row
        if not isinstance(row.get("review_text"), str) or not row.get("review_text", "").strip():
            errors.append(f"payload review {sample_id or index} has no review_text")
    return by_id


def normalize_extract_response(result: Any) -> Any:
    """Normalize common JSON-mode wrappers without dropping or inventing review evidence."""
    if isinstance(result, Mapping):
        return deepcopy(dict(result))
    if not isinstance(result, list) or not result or not all(isinstance(row, Mapping) for row in result):
        return result
    rows = [dict(row) for row in result]
    if len(rows) == 1 and set(rows[0]) == {"review_processing", "events"}:
        return deepcopy(rows[0])
    if all(set(row) == {"review_processing", "events"} for row in rows):
        return {
            "review_processing": [item for row in rows for item in row.get("review_processing", [])],
            "events": [item for row in rows for item in row.get("events", [])],
        }
    if all({"sample_id", "status", "reason", "events"}.issubset(row) for row in rows):
        processing, events = [], []
        for row in rows:
            review_events = row.get("events")
            if not isinstance(review_events, list):
                return result
            review = {key: row[key] for key in ("sample_id", "status", "reason")}
            review["event_count"] = row.get("event_count", len(review_events))
            processing.append(review)
            for event in review_events:
                if not isinstance(event, Mapping):
                    return result
                event_row = dict(event)
                event_row.setdefault("sample_id", row["sample_id"])
                events.append(event_row)
        return {"review_processing": processing, "events": events}
    return result


def validate_extract_result(payload: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    """Return deterministic contract errors; an empty list means structural checks pass.

    This does not certify semantic quality or replace independent human review.
    It verifies sample coverage, status/count agreement, event ownership/primary
    markers, minimum event fields, and source-locatable short quotations.
    """
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return ["payload must be an object"]
    if not isinstance(result, Mapping):
        return ["result must be an object"]
    expected = _review_map(payload, errors)
    if set(result) != {"review_processing", "events"}:
        errors.append("result must contain only review_processing and events")

    processing = result.get("review_processing")
    if not isinstance(processing, list):
        errors.append("result.review_processing must be an array")
        processing = []
    events = result.get("events")
    if not isinstance(events, list):
        errors.append("result.events must be an array")
        events = []

    processing_by_id: dict[str, Mapping[str, Any]] = {}
    processing_counts: Counter[str] = Counter()
    for index, row in enumerate(processing):
        if not isinstance(row, Mapping):
            errors.append(f"review_processing[{index}] must be an object")
            continue
        sample_id = _text(row.get("sample_id")).strip()
        if not sample_id:
            errors.append(f"review_processing[{index}].sample_id is required")
            continue
        processing_counts[sample_id] += 1
        if sample_id not in expected:
            errors.append(f"unknown review_processing sample_id: {sample_id}")
        if processing_counts[sample_id] == 1:
            processing_by_id[sample_id] = row
        else:
            errors.append(f"duplicate review_processing sample_id: {sample_id}")
        status = row.get("status")
        if status not in _STATUSES:
            errors.append(f"review_processing {sample_id} has invalid status")
        if not _text(row.get("reason")).strip():
            errors.append(f"review_processing {sample_id} requires a non-empty reason")
        count = row.get("event_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            errors.append(f"review_processing {sample_id} event_count must be a non-negative integer")

    for sample_id in expected:
        if processing_counts[sample_id] == 0:
            errors.append(f"missing review_processing sample_id: {sample_id}")

    event_counts: Counter[str] = Counter()
    primary_counts: Counter[str] = Counter()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(f"events[{index}] must be an object")
            continue
        if "event_id" in event:
            errors.append(f"events[{index}] must not contain event_id; IDs are assigned locally")
        sample_id = _text(event.get("sample_id")).strip()
        if not sample_id:
            errors.append(f"events[{index}].sample_id is required")
            continue
        if sample_id not in expected:
            errors.append(f"event references unknown sample_id: {sample_id}")
        event_counts[sample_id] += 1
        if event.get("primary_for_review") is True:
            primary_counts[sample_id] += 1
        elif event.get("primary_for_review") is not False:
            errors.append(f"event {sample_id} primary_for_review must be boolean")

        for group, fields in _EVENT_GROUPS.items():
            value = event.get(group)
            if not isinstance(value, Mapping):
                errors.append(f"event {sample_id} requires object field {group}")
                continue
            for field in fields:
                if not isinstance(value.get(field), str):
                    errors.append(f"event {sample_id} requires string field {group}.{field}")
        if event.get("confidence") not in _CONFIDENCE:
            errors.append(f"event {sample_id} has invalid confidence")

        quote = event.get("evidence_quote")
        source = _text(expected.get(sample_id, {}).get("review_text"))
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"event {sample_id} requires evidence_quote")
        else:
            if len(quote.strip()) > 160:
                errors.append(f"event {sample_id} evidence_quote exceeds 160 characters")
            if _normalized_space(quote) not in _normalized_space(source):
                errors.append(f"event {sample_id} evidence_quote is not a verbatim source substring")
        motivation = event.get("motivation")
        if isinstance(motivation, Mapping) and _text(motivation.get("inferred")).strip() and not _text(motivation.get("inference_basis")).strip():
            errors.append(f"event {sample_id} inferred motivation requires inference_basis")

    for sample_id in expected:
        row = processing_by_id.get(sample_id)
        count = event_counts[sample_id]
        if row is None:
            continue
        if row.get("event_count") != count:
            errors.append(f"review_processing {sample_id} event_count does not match events")
        if count:
            if row.get("status") != "processed_with_events":
                errors.append(f"review_processing {sample_id} status must be processed_with_events")
            if primary_counts[sample_id] != 1:
                errors.append(f"review {sample_id} must have exactly one primary event")
        else:
            if row.get("status") != "no_need_signal":
                errors.append(f"review_processing {sample_id} status must be no_need_signal")
            if primary_counts[sample_id] != 0:
                errors.append(f"review {sample_id} without events cannot have a primary event")
    for sample_id in event_counts:
        if sample_id not in expected:
            # Already reported above; this also makes uncovered ownership explicit.
            continue
    return errors


def assign_stable_event_ids(extraction_result: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy extraction output and add content-derived IDs independent of batch index."""
    result = deepcopy(dict(extraction_result))
    events = result.get("events")
    if not isinstance(events, list):
        raise ValueError("extraction_result.events must be an array")
    signatures: list[tuple[str, str]] = []
    per_signature: Counter[tuple[str, str]] = Counter()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError(f"events[{index}] must be an object")
        if "event_id" in event:
            raise ValueError(f"events[{index}] already contains event_id")
        sample_id = _text(event.get("sample_id")).strip()
        if not sample_id:
            raise ValueError(f"events[{index}] has no sample_id")
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(sample_id.encode("utf-8") + b"\0" + canonical.encode("utf-8")).hexdigest()[:24]
        signature = (sample_id, digest)
        signatures.append(signature)
        per_signature[signature] += 1

    occurrence: Counter[tuple[str, str]] = Counter()
    assigned: set[str] = set()
    for event, (sample_id, digest) in zip(events, signatures):
        signature = (sample_id, digest)
        occurrence[signature] += 1
        suffix = f"-{occurrence[signature]:02d}" if per_signature[signature] > 1 else ""
        event["event_id"] = f"EV2-{digest}{suffix}"
        if event["event_id"] in assigned:
            raise ValueError("stable event id collision")
        assigned.add(event["event_id"])
    return result
