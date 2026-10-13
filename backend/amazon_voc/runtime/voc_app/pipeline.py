from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ....local_result_cache import LocalResultCache, fingerprint
from ....usage_ledger import record_usage
from .db import utcnow
from .full_voc_v2 import (PROMPT_VERSION, adapt_result, assemble_full_result, build_dictionary_candidates, build_payload,
                          build_assignment_jobs, build_synthesis_payload, compact_synthesis_request,
                          minimum_formal_segment_reviews, normalize_v2_result, repair_assignment_batch,
                          repair_synthesis_contract, validate_assignment_batch, validate_dictionary_normalization,
                          validate_result, validate_synthesis_result, validate_synthesis_semantics)
from .full_voc_v2_extract import (assign_stable_event_ids, normalize_extract_response,
                                  validate_extract_result)
from .prompts import PromptRegistry
from .quality import FIELD_NAMESPACES, NAMESPACES, evaluate, iter_values
from .schemas import SCHEMAS


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
TERMINAL = {"completed", "failed", "cancelled"}
REVIEW_EVENTS_PER_CALL = 20
ASSIGNMENT_EVENTS_PER_CALL = 50
JOURNEY_EVENTS_PER_CALL = 100
MAPPING_REPAIR_VALUES_PER_CALL = 400
MAPPING_CANDIDATES_PER_CALL = 250
MAX_MAPPING_CANDIDATE_INPUT_BYTES = 192_000
MAX_DICTIONARY_MODEL_INPUT_BYTES = 192_000
MAX_SEGMENT_PROPOSAL_INPUT_BYTES = 96_000
MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES = 192_000
JOURNEY_STAGE_SAMPLES_PER_SEGMENT = 2
STAGE_RESULT_CACHE = LocalResultCache("voc-stage")


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stable(prefix, *values):
    digest = hashlib.sha1("\x1f".join(str(v or "").strip().casefold() for v in values).encode("utf-8")).hexdigest()[:10]
    return "%s-%s" % (prefix, digest.upper())


def _mapping_candidate_id(namespace, field_path, normalized_value):
    identity = json.dumps([namespace, field_path, normalized_value], ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return "MAP-" + hashlib.sha256(identity).hexdigest()[:24].upper()


def _normal_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


MODEL_EVENT_FIELDS = (
    "event_id", "evidence_quote", "audience", "scenario", "motivation", "task",
    "experience", "workaround", "confidence",
)
MAX_MODEL_EVIDENCE_QUOTE_CHARS = 160


def _drop_full_source_echo(value, normalized_source):
    if isinstance(value, dict):
        return {key: _drop_full_source_echo(item, normalized_source)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_drop_full_source_echo(item, normalized_source) for item in value]
    if (normalized_source and isinstance(value, str) and
            normalized_source in _normal_text(value)):
        return ""
    return value


def project_model_events(events):
    """Keep event evidence and stable identifiers while withholding full source reviews."""
    projected = []
    for event in events:
        normalized_source = _normal_text(event.get("review_text"))
        if len(normalized_source) < 80:
            normalized_source = ""
        row = {key: _drop_full_source_echo(event[key], normalized_source)
               for key in MODEL_EVENT_FIELDS if key in event}
        if isinstance(row.get("evidence_quote"), str):
            row["evidence_quote"] = row["evidence_quote"][:MAX_MODEL_EVIDENCE_QUOTE_CHARS]
        projected.append(row)
    return projected


def build_mapping_candidates(events):
    """Deduplicate repeated field values before asking the model for tag IDs."""
    candidates = {}
    seen_relations = set()
    for event in events:
        for field_path, raw_value, namespace in iter_values(event):
            normalized = _normal_text(raw_value)
            candidate_id = _mapping_candidate_id(namespace, field_path, normalized)
            candidate = candidates.setdefault(candidate_id, {
                "candidate_id": candidate_id, "field_path": field_path,
                "raw_value": raw_value, "namespace": namespace, "event_rows": [],
            })
            relation = (event.get("event_id"), field_path, normalized)
            if relation not in seen_relations:
                candidate["event_rows"].append({
                    "event_id": event.get("event_id"), "field_path": field_path,
                    "raw_value": raw_value,
                })
                seen_relations.add(relation)
    return [candidates[key] for key in sorted(candidates)]


def _exact_quote_fragment(quote, source):
    """Return a verbatim contiguous source fragment when a model joined excerpts with ellipses."""
    quote = str(quote or "")
    source = str(source or "")
    if not quote or not source:
        return None
    if _normal_text(quote) in _normal_text(source):
        return quote
    fragments = re.split(r"(?:\.{2,}|…+|⋯+)", quote)
    fragments.extend(re.split(r"(?<=[.!?])\s+", quote))
    for fragment in sorted(fragments, key=len, reverse=True):
        candidate = fragment.strip(" \t\r\n\"'“”‘’")
        if len(candidate) < 16:
            continue
        pattern = re.escape(candidate)
        pattern = re.sub(r"\\\s+", r"\\s+", pattern)
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def _repair_event_quotes(events):
    unresolved = []
    repaired = 0
    for event in events:
        quote = event.get("evidence_quote")
        source = event.get("review_text")
        if not quote or _normal_text(quote) not in _normal_text(source):
            exact = _exact_quote_fragment(quote, source)
            if exact:
                event["evidence_quote"] = exact
                repaired += 1
            else:
                unresolved.append(event.get("event_id"))
    return repaired, unresolved


def _chunks(rows, size):
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def _json_size_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_dictionary_batches(events, marketplace=None, category=None,
                            max_bytes=MAX_DICTIONARY_MODEL_INPUT_BYTES):
    """Split projected events into bounded vocabulary batches; isolate only individually oversized events."""
    max_bytes = max(1024, int(max_bytes or 1024))
    base = {"namespaces": NAMESPACES, "marketplace": marketplace, "category": category}

    def make_payload(rows):
        return {"events": rows, **base}

    batches, oversized_event_ids, current = [], [], []
    for event in project_model_events(events):
        event_id = event.get("event_id")
        if _json_size_bytes(make_payload([event])) > max_bytes:
            if event_id:
                oversized_event_ids.append(event_id)
            continue
        proposed = current + [event]
        if current and _json_size_bytes(make_payload(proposed)) > max_bytes:
            batches.append(current)
            current = [event]
        else:
            current = proposed
    if current:
        batches.append(current)
    return ([{"event_ids": [row.get("event_id") for row in rows],
              "payload": make_payload(rows)} for rows in batches], oversized_event_ids)


def merge_dictionary_batch_results(batches, results, event_by_id):
    """Merge equivalent tags across batch-local vocabularies using stable local IDs."""
    grouped = {}
    aliases_by_batch = {}
    for batch_index, (batch, result) in enumerate(zip(batches, results)):
        allowed_event_ids = set(batch["event_ids"])
        aliases = {}
        for source_tag in result.get("tags") or []:
            namespace = source_tag.get("namespace")
            canonical_name = str(source_tag.get("canonical_name") or "").strip()
            normalized_name = _normal_text(canonical_name)
            if namespace not in NAMESPACES or not normalized_name:
                continue
            support_ids = sorted({event_id for event_id in source_tag.get("support_event_ids") or []
                                  if event_id in allowed_event_ids and event_id in event_by_id})
            if not support_ids:
                continue
            stable_id = _stable("TAG", namespace, normalized_name)
            source_id = str(source_tag.get("tag_id") or "")
            if source_id:
                aliases[source_id] = stable_id
            key = (namespace, normalized_name)
            row = grouped.setdefault(key, {
                "tag_id": stable_id, "namespace": namespace, "canonical_name": canonical_name,
                "canonical_name_en": str(source_tag.get("canonical_name_en") or "").strip(),
                "definition": str(source_tag.get("definition") or "").strip(),
                "definition_en": str(source_tag.get("definition_en") or "").strip(),
                "parent_tag_id": "", "raw_variants": set(), "include_examples": set(),
                "exclude_examples": set(), "support_event_ids": set(), "confidence": "低",
                "_confidence_rank": 0, "_parent_refs": [],
            })
            row["support_event_ids"].update(support_ids)
            row["raw_variants"].update(str(value) for value in source_tag.get("raw_variants") or [] if str(value).strip())
            row["include_examples"].update(str(value) for value in source_tag.get("include_examples") or [] if str(value).strip())
            row["exclude_examples"].update(str(value) for value in source_tag.get("exclude_examples") or [] if str(value).strip())
            if not row["definition"]:
                row["definition"] = str(source_tag.get("definition") or "").strip()
            if not row["canonical_name_en"]:
                row["canonical_name_en"] = str(source_tag.get("canonical_name_en") or "").strip()
            if not row["definition_en"]:
                row["definition_en"] = str(source_tag.get("definition_en") or "").strip()
            rank = {"高": 3, "中": 2, "低": 1}.get(source_tag.get("confidence"), 0)
            if rank > row["_confidence_rank"]:
                row["confidence"] = source_tag.get("confidence")
                row["_confidence_rank"] = rank
            parent_id = str(source_tag.get("parent_tag_id") or "")
            if parent_id:
                row["_parent_refs"].append((batch_index, parent_id))
        aliases_by_batch[batch_index] = aliases

    output = []
    for row in grouped.values():
        for batch_index, source_parent_id in row.pop("_parent_refs"):
            resolved_parent_id = aliases_by_batch.get(batch_index, {}).get(source_parent_id)
            if resolved_parent_id and resolved_parent_id != row["tag_id"]:
                row["parent_tag_id"] = resolved_parent_id
                break
        row.pop("_confidence_rank", None)
        row["raw_variants"] = sorted(row["raw_variants"])
        row["include_examples"] = sorted(row["include_examples"])
        row["exclude_examples"] = sorted(row["exclude_examples"])
        row["support_event_ids"] = sorted(row["support_event_ids"])
        review_ids = {event_by_id[event_id].get("review_id") or event_id
                      for event_id in row["support_event_ids"]}
        row["independent_review_count"] = len(review_ids)
        row["status"] = "formal" if len(review_ids) >= 2 else "exploratory"
        row["normalization_status"] = "normalized"
        output.append(row)
    return sorted(output, key=lambda row: (row["namespace"], _normal_text(row["canonical_name"])))


def build_mapping_candidate_batches(candidates, tags,
                                    max_candidates=MAPPING_CANDIDATES_PER_CALL,
                                    max_bytes=MAX_MAPPING_CANDIDATE_INPUT_BYTES):
    """Batch unique values under a byte bound and include only relevant tag namespaces."""
    candidates = sorted(list(candidates or []), key=lambda row: (
        str(row.get("namespace") or ""), str(row.get("field_path") or ""),
        _normal_text(row.get("raw_value")), str(row.get("candidate_id") or "")))
    tags = list(tags or [])
    max_candidates = max(1, int(max_candidates or 1))
    max_bytes = max(1024, int(max_bytes or 1024))
    batches, current = [], []

    def make_payload(rows):
        namespaces = {row.get("namespace") for row in rows}
        compact_tags = [{key: tag.get(key, "") for key in
                         ("tag_id", "namespace", "canonical_name", "canonical_name_en",
                          "definition", "definition_en")}
                        for tag in tags if tag.get("namespace") in namespaces]
        payload = {"candidates": [{key: row.get(key, "") for key in
                                   ("candidate_id", "field_path", "raw_value", "namespace")}
                                   for row in rows],
                   "tag_dictionary": compact_tags, "field_namespace_map": FIELD_NAMESPACES}
        return payload

    for candidate in candidates:
        proposed = current + [candidate]
        if current and (candidate.get("namespace") != current[0].get("namespace") or
                        len(proposed) > max_candidates or
                        _json_size_bytes(make_payload(proposed)) > max_bytes):
            batches.append(current)
            current = [candidate]
        else:
            current = proposed
        if _json_size_bytes(make_payload(current)) > max_bytes:
            raise ValueError("单个词典映射候选超过安全输入上限")
    if current:
        batches.append(current)
    return [{"candidates": rows, "payload": make_payload(rows),
             "input_bytes": _json_size_bytes(make_payload(rows))} for rows in batches]


def _bounded_signal_text(value, limit):
    parts = []

    def visit(item):
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif item is not None and not isinstance(item, bool):
            text = re.sub(r"\s+", " ", str(item)).strip()
            if text:
                parts.append(text)

    visit(value)
    return "；".join(parts)[:max(0, int(limit))]


def build_journey_stage_proposal_input(events, segments,
                                       max_bytes=MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES,
                                       samples_per_segment=JOURNEY_STAGE_SAMPLES_PER_SEGMENT):
    """Build bounded, date-stratified comment signals for dynamic journey-stage discovery."""
    events = list(events or [])
    segments = list(segments or [])
    event_by_id = {event.get("event_id"): event for event in events if event.get("event_id")}
    samples_per_segment = max(1, int(samples_per_segment or 1))
    max_bytes = max(1024, int(max_bytes or MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES))

    def make_payload(sample_limit, text_limit):
        compact_segments = []
        sampled_count = 0
        for segment in segments:
            member_events = [event_by_id[event_id] for event_id in segment.get("member_event_ids") or []
                             if event_id in event_by_id]
            if not member_events:
                continue
            member_events.sort(key=lambda event: (str(event.get("review_date") or ""),
                                                   str(event.get("event_id") or "")))
            sample_count = min(sample_limit, len(member_events))
            if sample_count == 1:
                sample_indexes = [0]
            else:
                sample_indexes = sorted({round(index * (len(member_events) - 1) / (sample_count - 1))
                                         for index in range(sample_count)})
            signals = []
            for index in sample_indexes:
                event = member_events[index]
                signals.append({
                    "event_id": str(event.get("event_id") or ""),
                    "scenario": _bounded_signal_text(event.get("scenario"), text_limit),
                    "motivation": _bounded_signal_text(event.get("motivation"), text_limit),
                    "job": _bounded_signal_text(event.get("task"), text_limit),
                    "experience": _bounded_signal_text(event.get("experience"), text_limit),
                    "workaround": _bounded_signal_text(event.get("workaround"), text_limit),
                    "evidence_quote": _bounded_signal_text(event.get("evidence_quote"), text_limit),
                })
            sampled_count += len(signals)
            compact_segments.append({
                "segment_id": str(segment.get("segment_id") or ""),
                "name": _bounded_signal_text(segment.get("name"), text_limit),
                "persona_summary": _bounded_signal_text(segment.get("persona_summary"), text_limit * 2),
                "jtbd_statement": _bounded_signal_text(segment.get("jtbd_statement"), text_limit * 2),
                "member_event_count": len(member_events),
                "review_signals": signals,
            })
        payload = {
            "event_count": len(events),
            "sampling_method": "evenly_spaced_by_review_date_within_each_active_segment",
            "max_samples_per_segment": sample_limit,
            "segments": compact_segments,
        }
        return payload, sampled_count

    for sample_limit, text_limit in ((samples_per_segment, 112), (1, 80), (1, 48), (1, 24)):
        payload, sampled_count = make_payload(sample_limit, text_limit)
        if _json_size_bytes(payload) <= max_bytes:
            payload["sampling"] = {
                "sampled_event_count": sampled_count,
                "active_segment_count": len(payload["segments"]),
                "max_samples_per_segment": sample_limit,
                "text_limit_chars": text_limit,
            }
            if _json_size_bytes(payload) <= max_bytes:
                return payload
    raise ValueError("旅程阶段样本摘要超过安全输入上限")


def build_segment_proposal_batches(proposal_input, max_bytes=MAX_SEGMENT_PROPOSAL_INPUT_BYTES):
    """Split complete profiles into bounded requests without truncating evidence."""
    profiles = list(proposal_input.get("profiles") or [])
    if not profiles:
        return []
    fixed = {key: value for key, value in proposal_input.items() if key not in ("profiles", "coverage")}
    full_coverage = proposal_input.get("coverage") or {}
    profile_limit = max(1, max_bytes - 1024)
    profile_chunks = []
    current = []
    for profile in profiles:
        if _json_size_bytes({**fixed, "profiles": [profile]}) > profile_limit:
            raise ValueError("单个人群画像超过细分提案输入上限，已阻止超限请求")
        candidate = current + [profile]
        if current and _json_size_bytes({**fixed, "profiles": candidate}) > profile_limit:
            profile_chunks.append(current)
            current = []
        current.append(profile)
    if current:
        profile_chunks.append(current)

    result = []
    for index, chunk in enumerate(profile_chunks, 1):
        chunk_event_count = sum(int(profile.get("e") or 0) for profile in chunk)
        payload = {
            **fixed,
            "profiles": chunk,
            "coverage": {
                "event_count": chunk_event_count,
                "profile_count": len(chunk),
                "all_events_included": True,
                "review_count_is_not_additive_across_profiles": True,
                "full_event_count": int(full_coverage.get("event_count") or 0),
                "full_profile_count": int(full_coverage.get("profile_count") or len(profiles)),
                "proposal_batch_index": index,
                "proposal_batch_count": len(profile_chunks),
            },
        }
        result.append({"payload": payload, "profile_count": len(chunk),
                       "event_count": chunk_event_count, "input_bytes": _json_size_bytes(payload)})
    return result


def _profile_value(value):
    if isinstance(value, dict):
        return {key: _profile_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_profile_value(item) for item in value if str(item or "").strip()]
    return str(value or "").strip()


def build_segment_proposal_input(events, dictionary, minimum_formal_reviews):
    """Create a bounded evidence profile instead of sending full events and mappings."""
    field_groups = (
        ("audience", ("user", "relationship", "physical_constraints", "skill_level", "role_needs")),
        ("scenario", ("context", "place", "environmental_stressors", "social_emotional_context", "trigger")),
        ("motivation", ("explicit", "inferred")),
        ("task", ("core_job", "functional_job", "emotional_job", "social_job", "desired_outcomes")),
    )
    signal_fields = (
        ("体验对象", "experience", "object"), ("组件", "experience", "component"),
        ("属性", "experience", "attribute"), ("结果", "experience", "result"),
        ("痛点", "experience", "pain"), ("痒点", "experience", "itch"),
        ("目标结果", "task", "desired_outcomes"),
        ("用户自救动作", "workaround", "standardized_action"),
    )

    def at(event, group, field):
        value = (event.get(group) or {}).get(field, "")
        return _profile_value(value)

    def identity(value):
        if isinstance(value, dict):
            return {key: identity(item) for key, item in value.items()}
        if isinstance(value, list):
            return [identity(item) for item in value]
        return _normal_text(value)

    groups = {}
    for event in events:
        dimensions = [[at(event, group, field) for field in fields] for group, fields in field_groups]
        has_dimensions = any(_normal_text(item) for group in dimensions for value in group
                             for item in (value if isinstance(value, list) else [value]))
        fallback = None
        if not has_dimensions:
            fallback = [at(event, "experience", field)
                        for field in ("object", "component", "attribute", "result", "pain", "itch")]
            fallback.append(at(event, "workaround", "standardized_action"))
        key = json.dumps({"dimensions": identity(dimensions), "fallback": identity(fallback)},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        profile = groups.get(key)
        if profile is None:
            profile = {
                "dimensions": dimensions,
                "fallback": fallback,
                "event_count": 0,
                "_review_ids": set(),
                "_signals": {},
                "_examples": [],
            }
            groups[key] = profile
        profile["event_count"] += 1
        review_id = str(event.get("review_id") or "").strip()
        if review_id:
            profile["_review_ids"].add(review_id)
        event_id = str(event.get("event_id") or "").strip()
        for label, group, field in signal_fields:
            value = at(event, group, field)
            values = value if isinstance(value, list) else [value]
            for raw in values:
                normalized = _normal_text(raw)
                if normalized:
                    signal_key = (label, normalized)
                    entry = profile["_signals"].setdefault(signal_key, {"type": label, "value": raw, "event_count": 0})
                    entry["event_count"] += 1
        quote = str(event.get("evidence_quote") or "").strip()
        if quote and not profile["_examples"]:
            profile["_examples"].append([event_id, review_id,
                                         quote[:120]])

    profiles = []
    for profile in groups.values():
        row = {"d": profile["dimensions"], "e": profile["event_count"],
               "r": len(profile["_review_ids"]),
               "n": [[signal["type"], signal["value"], signal["event_count"]]
                     for signal in sorted(profile["_signals"].values(),
                                          key=lambda item: (-item["event_count"], item["type"], item["value"]))[:3]],
               "q": profile["_examples"]}
        if profile["fallback"]:
            row["f"] = profile["fallback"]
        profiles.append(row)
    profiles.sort(key=lambda row: (-row["r"], -row["e"], json.dumps(row["d"], ensure_ascii=False)))
    namespaces = {"audience", "scenario", "motivation", "task", "desired_outcome", "object", "component",
                  "attribute", "result", "pain", "itch", "workaround_action"}
    tag_catalog = [{key: tag.get(key, "") for key in
                    ("namespace", "canonical_name", "canonical_name_en", "definition", "definition_en")}
                   for tag in dictionary.get("tags") or []
                   if tag.get("namespace") in namespaces and tag.get("normalization_status") != "raw_unmerged"]
    return {
        "profiles": profiles,
        "tag_catalog": tag_catalog,
        "minimum_formal_reviews": minimum_formal_reviews,
        "coverage": {"event_count": len(events), "profile_count": len(profiles),
                     "all_events_included": True,
                     "review_count_is_not_additive_across_profiles": True},
    }


def _schema_errors(value, schema, path="$", limit=20):
    errors = []
    def walk(item, rule, current):
        if len(errors) >= limit:
            return
        if "anyOf" in rule:
            if not any(not _schema_errors(item, candidate, current, 1) for candidate in rule["anyOf"]):
                errors.append(current + " does not match anyOf")
            return
        kind = rule.get("type")
        valid = ((kind == "object" and isinstance(item, dict)) or
                 (kind == "array" and isinstance(item, list)) or
                 (kind == "string" and isinstance(item, str)) or
                 (kind == "integer" and isinstance(item, int) and not isinstance(item, bool)) or
                 (kind == "boolean" and isinstance(item, bool)) or
                 (kind == "null" and item is None) or kind is None)
        if not valid:
            errors.append("%s expected %s, received %s" % (current, kind, type(item).__name__)); return
        if "enum" in rule and item not in rule["enum"]:
            errors.append("%s invalid enum" % current)
        if isinstance(item, dict):
            for key in rule.get("required") or []:
                if key not in item:
                    errors.append("%s.%s missing" % (current, key))
            if rule.get("additionalProperties") is False:
                for key in set(item) - set(rule.get("properties") or {}):
                    errors.append("%s.%s unexpected" % (current, key))
            for key, child in (rule.get("properties") or {}).items():
                if key in item: walk(item[key], child, current + "." + key)
        if isinstance(item, list):
            if "maxItems" in rule and len(item) > rule["maxItems"]:
                errors.append("%s too many items" % current)
            for index, child in enumerate(item):
                walk(child, rule.get("items", {}), "%s[%s]" % (current, index))
    walk(value, schema, path)
    return errors


def _is_schema_echo(value, schema):
    """Return True when a model response is the JSON Schema document itself."""
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return False
    properties = schema.get("properties") or {}
    expected_keys = set(properties) if isinstance(properties, dict) else set()
    schema_keys = {"$schema", "$id", "title", "type", "properties", "required", "additionalProperties"}
    return bool(set(value) & schema_keys) and not (set(value) & expected_keys)


def _cacheable_stage_result(task, payload, result, schema=None):
    """Cross-run reuse requires complete stage coverage, not schema validity alone."""
    if not isinstance(result, dict):
        return False
    if task == "full_voc_v2":
        return not validate_result(payload, result)
    if task == "full_voc_v2_extract":
        return not validate_extract_result(payload, result)
    if task == "full_voc_v2_synthesis":
        return not _schema_errors(result, schema or {}) and not validate_synthesis_semantics(payload, result)
    if task == "full_voc_v2_dictionary":
        return not _schema_errors(result, schema or {}) and not validate_dictionary_normalization(payload, result)
    if task == "full_voc_v2_assign":
        return (not _schema_errors(result, schema or {})
                and not validate_assignment_batch(payload, result))
    if task == "extract_events":
        expected = {row.get("review_id") for row in payload.get("reviews", [])}
        actual = {row.get("review_id") for row in result.get("reviews", [])}
        return bool(expected) and actual == expected
    if task == "review_events":
        expected = {row.get("event_id") for row in payload.get("events", [])}
        actual = {row.get("event_id") for row in result.get("decisions", [])}
        return actual == expected
    if task == "assign_segments":
        expected = set(payload.get("required_event_ids") or
                       [row.get("event_id") for row in payload.get("events", [])])
        assignments = result.get("assignments", [])
        actual = {row.get("event_id") for row in assignments}
        allowed = {row.get("segment_id") for row in payload.get("segments", [])}
        return actual == expected and all(row.get("segment_id") in allowed for row in assignments)
    if task == "build_journey":
        if not payload.get("events"):
            return bool(result.get("stages"))
        expected = set(payload.get("required_event_ids") or
                       [row.get("event_id") for row in payload.get("events", [])])
        assignments = result.get("assignments", [])
        actual = {row.get("event_id") for row in assignments}
        allowed = {row.get("stage_id") for row in payload.get("fixed_stages", [])}
        return actual == expected and all(row.get("stage_id") in allowed for row in assignments)
    if task == "map_dictionary":
        expected = {row.get("candidate_id") for row in payload.get("candidates", [])}
        rows = result.get("mappings", [])
        actual = {row.get("candidate_id") for row in rows}
        allowed = {row.get("tag_id") for row in payload.get("tag_dictionary", [])}
        return (actual == expected and len(rows) == len(expected) and
                all(row.get("tag_id") in allowed for row in rows))
    if task == "propose_segments":
        return bool(result.get("segments"))
    return True


def _batches(rows, max_reviews, max_chars):
    batch, chars = [], 0
    for row in rows:
        size = len(str(row.get("content") or ""))
        if size > max_chars:
            raise ValueError("单条评论超过批次字符上限：%s > %s (%s)" %
                             (size, max_chars, row.get("review_id")))
        if batch and (len(batch) >= max_reviews or chars + size > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(row); chars += size
    if batch:
        yield batch


def _full_voc_v2_batches(rows, max_reviews, max_chars):
    """Chunk V2 reviews by both count and source-text size, retaining stable input order."""
    batch, chars = [], 0
    for row in rows:
        size = len(" ".join(str(row.get(key) or "") for key in ("title", "text")))
        if size > max_chars:
            raise ValueError("单条评论超过 V2 批次字符上限：%s > %s (%s)" %
                             (size, max_chars, row.get("sample_id")))
        if batch and (len(batch) >= max_reviews or chars + size > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(row)
        chars += size
    if batch:
        yield batch


def _full_voc_v2_extract_payload(rows, task_settings):
    reviews = [{
        "sample_id": row["sample_id"], "asin": row.get("asin", ""),
        "rating": row.get("rating", ""), "review_date": row.get("review_date", ""),
        "variant": row.get("variant", ""), "review_title": row.get("title", ""),
        "review_text": row.get("text", ""),
    } for row in rows]
    return {"sample_count": len(reviews), "task_settings": dict(task_settings or {}), "reviews": reviews}


def _empty_full_voc_v2_synthesis(sample_count):
    return {
        "overview": {"zh": "本样本未抽取到可由评论证据支持的 VOC 事件。",
                     "en": "No VOC events supported by the review evidence were extracted."},
        "method_note": {"zh": "评论已分块逐条处理并完成覆盖校验；本次没有事件可供需求综合。",
                        "en": "Reviews were processed in validated chunks; no events were available for synthesis."},
        "tag_dictionary": [], "value_tag_mappings": [], "segments": [],
        "event_assignments": [], "segment_comparison": [],
        "journey": {
            "interpretation_type": "Cross-sectional synthesis of experience stages",
            "summary": {"zh": "没有足够的事件证据形成体验阶段。", "en": "There is not enough event evidence to form experience stages."},
            "limitations": {"zh": "无事件不代表用户不存在相关需求，只代表本次评论未提供可抽取证据。",
                            "en": "No extracted events do not prove that needs are absent; this sample provided no extractable evidence."},
            "stages": [],
        },
        "cross_cutting": [], "exploratory_signals": [],
        "limitations": [
            {"zh": "本次覆盖 %s 条评论；未观察到可抽取事件不等于相关需求不存在。" % sample_count,
             "en": "This run covered %s reviews; no extracted events does not mean related needs do not exist." % sample_count},
            {"zh": "结果只描述输入样本，不代表市场规模、份额或因果关系。",
             "en": "Results describe only the input sample and do not represent market size, share, or causality."},
        ],
    }


class Pipeline:
    def __init__(self, settings, db, storage, provider, prompts=None):
        self.settings = settings
        self.db = db
        self.storage = storage
        self.provider = provider
        self.prompts = prompts or PromptRegistry()

    def _status(self, run_id, status):
        run = self.db.get_run(run_id)
        if run and run.get("cancel_requested"):
            self.db.update_run(run_id, status="cancelled", completed_at=utcnow())
            raise InterruptedError("任务已取消")
        self.db.update_run(run_id, status=status, error="")

    @staticmethod
    def _coverage_round(workdir, task):
        rounds = _read(Path(workdir) / "cache" / "coverage_rounds.json", {}) or {}
        return int(rounds.get(task, 0))

    @staticmethod
    def _advance_coverage_round(workdir, task):
        path = Path(workdir) / "cache" / "coverage_rounds.json"
        rounds = _read(path, {}) or {}
        rounds[task] = int(rounds.get(task, 0)) + 1
        _write(path, rounds)
        return rounds[task]

    def _call(self, run, revision, workdir, task, batch_id, payload, reviewer=False, schema_override=None):
        prompt_meta, instructions = self.prompts.load(task)
        is_full_voc_v2_task = task.startswith("full_voc_v2")
        if is_full_voc_v2_task:
            instructions = instructions.replace("{sample_count}", str(int(payload.get("sample_count") or 0)))
            instructions = instructions.replace(
                "{formal_threshold}", str(minimum_formal_segment_reviews(payload.get("sample_count") or 0))
            )
        schema = schema_override or prompt_meta.get("schema") or SCHEMAS[task]
        base_digest = _hash({"prompt_version": prompt_meta["version"], "instructions": instructions,
                             "schema": schema, "payload": payload,
                             "model": self.settings.reviewer_model if reviewer else self.settings.extractor_model})
        existing = self.db.one("SELECT * FROM batches WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                               (run["id"], revision, task, batch_id))
        if existing and existing["input_hash"] == base_digest:
            cached = Path(existing["output_path"])
            if cached.exists():
                checkpoint = _read(cached)
                if task == "full_voc_v2_synthesis":
                    checkpoint_valid = (isinstance(checkpoint, dict)
                                         and not _schema_errors(checkpoint, schema)
                                         and not validate_synthesis_semantics(payload, checkpoint))
                elif task == "full_voc_v2_assign":
                    checkpoint_valid = (isinstance(checkpoint, dict)
                                         and not _schema_errors(checkpoint, schema)
                                         and not validate_assignment_batch(payload, checkpoint))
                elif task == "full_voc_v2_dictionary":
                    checkpoint_valid = (isinstance(checkpoint, dict)
                                         and not _schema_errors(checkpoint, schema)
                                         and not validate_dictionary_normalization(payload, checkpoint))
                else:
                    checkpoint_valid = (isinstance(checkpoint, dict) if not is_full_voc_v2_task else
                        isinstance(checkpoint, dict) and not _schema_errors(checkpoint, schema)
                        and _cacheable_stage_result(task, payload, checkpoint, schema))
                if checkpoint_valid:
                    self.db.execute("UPDATE batches SET status='completed',updated_at=? "
                                    "WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                                    (utcnow(), run["id"], revision, task, batch_id))
                    record_usage("voc", run["id"], run.get("workspace_id"), {
                        "stage": task, "model": self.settings.reviewer_model if reviewer else self.settings.extractor_model,
                        "status": "run_checkpoint_hit",
                        "local_cache_hit": True, "request": False, "retries": 0,
                    })
                    return checkpoint
        output_path = Path(workdir) / "cache" / "batches" / task / (batch_id + ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cache_settings = {"model_protocol": self.settings.model_protocol,
                          "model_base_url": self.settings.model_base_url,
                          "max_repairs": self.settings.max_repairs,
                          "batch_reviews": self.settings.batch_reviews,
                          "batch_chars": self.settings.batch_chars,
                          "full_voc_timeout_seconds": self.settings.full_voc_timeout_seconds}
        source_options = run.get("sources") or {}
        review_context = source_options.get("review_source_context") or {}
        forced_revisions = {int(value) for value in source_options.get("force_refresh_revisions", [])
                            if str(value).isdigit()}
        force_refresh = bool(source_options.get("force_refresh") or review_context.get("force_refresh")
                             or int(revision) in forced_revisions)
        cache_models = ([self.settings.extractor_model] if is_full_voc_v2_task else
                        [self.settings.reviewer_model] if reviewer else
                        list(dict.fromkeys([self.settings.extractor_model, self.settings.reviewer_model])))
        cache_keys = {model_name: fingerprint({"version": "voc-stage-cache-v2", "stage": task,
            "prompt_version": prompt_meta["version"], "instructions": instructions, "schema": schema,
            "payload": payload, "model": model_name, "settings": cache_settings})
            for model_name in cache_models}
        if not force_refresh:
            for model_name, cache_key in cache_keys.items():
                cache_hit, cached_result = STAGE_RESULT_CACHE.get(run.get("workspace_id"), cache_key)
                cache_valid = (isinstance(cached_result, dict) and
                    (_cacheable_stage_result(task, payload, cached_result, schema)
                     if task in ("full_voc_v2_synthesis", "full_voc_v2_assign", "full_voc_v2_dictionary") else
                     not _schema_errors(cached_result, schema)
                     and _cacheable_stage_result(task, payload, cached_result, schema)))
                if cache_hit and cache_valid:
                    _write(output_path, cached_result)
                    self.db.execute("INSERT INTO batches(run_id,revision,stage,batch_id,input_hash,status,output_path,attempts,updated_at) "
                        "VALUES(?,?,?,?,?,'completed',?,0,?) ON CONFLICT(run_id,revision,stage,batch_id) DO UPDATE SET "
                        "input_hash=excluded.input_hash,status='completed',output_path=excluded.output_path,attempts=0,updated_at=excluded.updated_at",
                        (run["id"], revision, task, batch_id, base_digest, str(output_path), utcnow()))
                    self.db.record_model_call(run["id"], revision, task, batch_id, cache_key, model_name,
                        prompt_meta["version"], self.settings.model_protocol, 0, None, None, 0,
                        "local_cache_hit")
                    record_usage("voc", run["id"], run.get("workspace_id"), {
                        "stage": task, "model": model_name, "status": "local_cache_hit",
                        "local_cache_hit": True, "request": False, "retries": 0,
                    })
                    return cached_result
        record_usage("voc", run["id"], run.get("workspace_id"), {
            "stage": task, "model": cache_models[0] if cache_models else "", "status": "cache_miss",
            "local_cache_miss": True, "request": False, "retries": 0,
        })
        now = utcnow()
        self.db.execute("INSERT INTO batches(run_id,revision,stage,batch_id,input_hash,status,output_path,attempts,updated_at) "
                        "VALUES(?,?,?,?,?,'running',?,0,?) ON CONFLICT(run_id,revision,stage,batch_id) DO UPDATE SET "
                        "input_hash=excluded.input_hash,status='running',output_path=excluded.output_path,updated_at=excluded.updated_at",
                        (run["id"], revision, task, batch_id, base_digest, str(output_path), now))
        errors = []
        attempts = 2 if task == "full_voc_v2_extract" else (
            1 if is_full_voc_v2_task else self.settings.max_repairs + 2)
        attempts_made = 0
        retryable_error = True
        schema_echo_retry = False
        for attempt in range(attempts):
            attempts_made = attempt + 1
            model = (self.settings.extractor_model if is_full_voc_v2_task else
                     self.settings.reviewer_model if reviewer or attempt == attempts - 1 else self.settings.extractor_model)
            request_payload = payload
            if errors and task != "full_voc_v2_extract":
                request_payload = {"original_input": payload, "validation_errors": errors,
                                   "repair_instruction": "修复结构并返回完整结果"}
            request_instructions = instructions
            if schema_echo_retry:
                request_instructions += (
                    "\n纠错重试：上一轮把 JSON Schema 定义本身当作结果返回了。"
                    "本次请依据 reviews 抽取并返回一个数据实例，顶层只包含 review_processing 和 events；"
                    "不要复述、复制或输出 JSON Schema。")
            allowed_segment_ids = (((schema.get("properties") or {}).get("assignments") or {}).get("items") or {}).get("properties", {}).get("segment_id", {}).get("enum")
            if errors and allowed_segment_ids:
                request_payload["repair_instruction"] += "；segment_id 只能使用这些候选 ID：" + json.dumps(allowed_segment_ids, ensure_ascii=False)
            started = datetime.now(timezone.utc)
            try:
                try:
                    provider_payload = (compact_synthesis_request(payload)
                                        if task == "full_voc_v2_synthesis"
                                        else request_payload)
                    result, usage = self.provider.call(task, model, request_instructions, provider_payload, schema)
                    call_status = "completed"
                    call_error = ""
                except Exception as exc:
                    result, usage = None, getattr(exc, "usage", {}) or {}
                    call_status = "failed"
                    call_error = str(exc)
                    raise
                finally:
                    usage_row = usage if isinstance(usage, dict) else {}
                    call_id = self.db.record_model_call(run["id"], revision, task, batch_id,
                        cache_keys.get(model, fingerprint({"stage": task, "model": model, "payload": payload})),
                        model, prompt_meta["version"], self.settings.model_protocol,
                        int(usage_row.get("latency_ms") or 0), usage_row.get("input_tokens"),
                        usage_row.get("output_tokens"), attempt, call_status, call_error[:2000],
                        cache_hit_input_tokens=usage_row.get("cache_hit_input_tokens"),
                        cache_miss_input_tokens=usage_row.get("cache_miss_input_tokens"))
                    record_usage("voc", run["id"], run.get("workspace_id"), {
                        "stage": task, "model": model, "status": call_status, "request": True,
                        "input_tokens": usage_row.get("input_tokens"), "output_tokens": usage_row.get("output_tokens"),
                        "cache_hit_input_tokens": usage_row.get("cache_hit_input_tokens"),
                        "cache_miss_input_tokens": usage_row.get("cache_miss_input_tokens"),
                        "retries": attempt,
                    })
                if task == "full_voc_v2_extract":
                    result = normalize_extract_response(result)
                schema_errors = _schema_errors(result, schema)
                errors = schema_errors
                schema_echo_retry = (task == "full_voc_v2_extract" and attempt == 0
                                     and _is_schema_echo(result, schema))
                if not schema_errors and task == "full_voc_v2_extract":
                    errors.extend(validate_extract_result(payload, result))
                elif not schema_errors and task == "full_voc_v2_synthesis":
                    errors.extend(validate_synthesis_semantics(payload, result))
                elif not schema_errors and task == "full_voc_v2_assign":
                    result, assignment_repair_summary = repair_assignment_batch(payload, result)
                    repair_path = output_path.with_suffix(".repairs.json")
                    _write(repair_path, assignment_repair_summary)
                    errors.extend(validate_assignment_batch(payload, result))
                elif not schema_errors and task == "full_voc_v2_dictionary":
                    errors.extend(validate_dictionary_normalization(payload, result))
                status, error_text = ("completed", "") if not errors else ("invalid", "; ".join(errors[:8]))
                if errors and call_id:
                    self.db.update_model_call(call_id, status="invalid", error=error_text[:2000])
                retryable_error = True
            except Exception as exc:
                result, usage, errors, status, error_text = None, {}, [str(exc)], "failed", str(exc)
                retryable_error = bool(getattr(exc, "retryable", True))
            latency = usage.get("latency_ms") if usage else None
            if latency is None:
                latency = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            if result is not None and not errors:
                _write(output_path, result)
                cache_key = cache_keys.get(model)
                if cache_key and _cacheable_stage_result(task, payload, result, schema):
                    STAGE_RESULT_CACHE.put(run.get("workspace_id"), cache_key, result)
                self.db.execute("UPDATE batches SET status='completed',attempts=?,updated_at=? WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                                (attempt + 1, utcnow(), run["id"], revision, task, batch_id))
                return result
            if result is None and not retryable_error:
                break
            if errors and is_full_voc_v2_task and not schema_echo_retry:
                break
        self.db.execute("UPDATE batches SET status='failed',attempts=?,updated_at=? WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                        (attempts_made, utcnow(), run["id"], revision, task, batch_id))
        raise ValueError("%s/%s 连续结构校验失败: %s" % (task, batch_id, "; ".join(errors[:8])))

    def _call_many(self, run, revision, workdir, task, jobs, reviewer=False, fail_fast=False):
        """Run independent model batches using the worker's configured bounded concurrency."""
        jobs = list(jobs)
        if not jobs:
            return []
        workers = max(1, min(int(getattr(self.settings, "concurrency", 1) or 1), len(jobs)))
        if workers == 1:
            return [self._call(run, revision, workdir, task, batch_id, payload, reviewer=reviewer)
                    for batch_id, payload in jobs]
        outputs = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._call, run, revision, workdir, task, batch_id, payload, reviewer): index
                       for index, (batch_id, payload) in enumerate(jobs)}
            for future in as_completed(futures):
                try:
                    outputs[futures[future]] = future.result()
                except Exception:
                    if fail_fast:
                        for pending in futures:
                            if pending is not future:
                                pending.cancel()
                    raise
        return outputs

    def _export_calls(self, run, revision, workdir):
        rows = self.db.all("SELECT stage,batch_id,input_hash,model,prompt_version,protocol,latency_ms,input_tokens,"
                           "output_tokens,cache_hit_input_tokens,cache_miss_input_tokens,retries,status,error,created_at "
                           "FROM model_calls WHERE run_id=? AND revision=? ORDER BY id",
                           (run["id"], revision))
        _write(Path(workdir) / "cache" / "model_calls.json", {"schema": "amazon-voc-model-calls/v1", "calls": rows})

    def _run_script(self, name, *args):
        env = os.environ.copy(); env["PYTHONUTF8"] = "1"
        result = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr).strip())
        return result.stdout

    def _initialize(self, run, workdir):
        sources = run["sources"]
        args = ["--reviews", sources["reviews"], "--workdir", workdir,
                "--marketplace", run["marketplace"], "--country", run["country"],
                "--language", run["language"], "--category", run["category"], "--task-id", run["id"]]
        self._run_script("load_input.py", *args)
        self._run_script("clean_data.py", "--workdir", workdir)

    def _extract(self, run, revision, workdir):
        cleaned = _read(Path(workdir) / "cache" / "cleaned_reviews.json", {"rows": []})["rows"]
        master = _read(Path(workdir) / "cache" / "master.json", {})
        batches = list(_batches(cleaned, self.settings.batch_reviews, self.settings.batch_chars))
        outputs = {}
        def execute(index, rows):
            payload = {"marketplace": master.get("marketplace"), "category": master.get("category"), "reviews": rows}
            return index, self._call(run, revision, workdir, "extract_events", "batch-%05d" % index, payload)
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            futures = [pool.submit(execute, i, rows) for i, rows in enumerate(batches, 1)]
            for future in as_completed(futures):
                index, output = future.result(); outputs[index] = output
        events, processing = [], []
        review_by_id = {r["review_id"]: r for r in cleaned}
        for index in sorted(outputs):
            for review_result in outputs[index].get("reviews") or []:
                review_id = review_result["review_id"]
                source = review_by_id.get(review_id, {})
                rows = review_result.get("events") or []
                processing.append({"review_id": review_id, "status": review_result.get("status"),
                                   "reason": review_result.get("no_signal_reason", ""), "event_count": len(rows)})
                for ordinal, event in enumerate(rows, 1):
                    event = dict(event)
                    event["event_id"] = "%s-事%02d" % (review_id, ordinal)
                    event["review_id"] = review_id
                    event["asin"] = source.get("asin", "")
                    event["rating"] = source.get("rating", "")
                    event["review_date"] = source.get("review_date", "")
                    event["review_text"] = source.get("content", "")
                    events.append(event)
        metadata = {"model": self.settings.extractor_model, "model_version": self.settings.extractor_model,
                    "prompt_version": self.prompts.load("extract_events")[0]["version"], "generated_at": utcnow()}
        _write(Path(workdir) / "cache" / "events.json", {"schema": "amazon-voc-events/v4", "metadata": metadata, "events": events})
        _write(Path(workdir) / "cache" / "review_processing.json",
               {"schema": "amazon-voc-review-processing/v1", "reviews": processing})

    def _reconcile_extraction_coverage(self, run, revision, workdir):
        """Recover event-backed processing rows and re-extract omitted reviews only."""
        cache = Path(workdir) / "cache"
        cleaned = (_read(cache / "cleaned_reviews.json", {"rows": []}) or {}).get("rows") or []
        events_doc = _read(cache / "events.json", {"events": [], "metadata": {}}) or {}
        events = events_doc.get("events") or []
        processing_doc = _read(cache / "review_processing.json", {"reviews": []}) or {}
        processing = []
        processing_signatures = {}
        for row in processing_doc.get("reviews") or []:
            review_id = row.get("review_id")
            signature = (review_id, row.get("status"), row.get("reason"), row.get("event_count"))
            if review_id and signature in processing_signatures:
                # Identical duplicate rows are a recoverable cache artifact. Conflicting
                # rows remain visible to the quality gate for manual diagnosis.
                continue
            if review_id:
                processing_signatures[signature] = True
            processing.append(row)
        cleaned_by_id = {row.get("review_id"): row for row in cleaned if row.get("review_id")}
        event_counts = {}
        for event in events:
            review_id = event.get("review_id")
            if review_id in cleaned_by_id:
                event_counts[review_id] = event_counts.get(review_id, 0) + 1

        # If events survived a prior interruption but their processing row did not,
        # reconstruct that row from the event cache without making a model call.
        processing_by_id = {}
        for row in processing:
            review_id = row.get("review_id")
            if review_id and review_id not in processing_by_id:
                processing_by_id[review_id] = row
        for review in cleaned:
            review_id = review.get("review_id")
            if not review_id:
                continue
            row = processing_by_id.get(review_id)
            if event_counts.get(review_id) and (
                    not row or row.get("status") != "已生成事件" or
                    int(row.get("event_count") or 0) != event_counts[review_id]):
                recovered = {"review_id": review_id, "status": "已生成事件",
                             "reason": "由已有语义事件缓存恢复处理记录",
                             "event_count": event_counts[review_id]}
                if row:
                    row.update(recovered)
                else:
                    processing.append(recovered)
                    processing_by_id[review_id] = recovered

        def pending_reviews():
            rows = []
            for review in cleaned:
                review_id = review.get("review_id")
                processing_row = processing_by_id.get(review_id)
                if (not processing_row or processing_row.get("status") == "失败" or
                        (processing_row.get("status") == "已生成事件" and not event_counts.get(review_id))):
                    rows.append(review)
            return rows

        pending = pending_reviews()
        coverage_round = self._coverage_round(workdir, "extraction")
        for attempt in range(2):
            if not pending:
                break
            for batch_number, chunk in enumerate(_chunks(pending, max(1, int(self.settings.batch_reviews))), 1):
                requested_ids = {row["review_id"] for row in chunk}
                batch_id = "extract-coverage-r%02d-%05d-a%02d" % (coverage_round, batch_number, attempt + 1)
                payload = {"marketplace": (_read(cache / "master.json", {}) or {}).get("marketplace"),
                           "category": (_read(cache / "master.json", {}) or {}).get("category"),
                           "reviews": chunk,
                           "required_review_ids": sorted(requested_ids),
                           "selection_reason": "补齐已清洗评论的抽取处理记录；每个 required_review_id 必须返回一次明确结果。"}
                try:
                    result = self._call(run, revision, workdir, "extract_events", batch_id, payload)
                except Exception:
                    # Keep the unresolved reviews explicit below; a provider failure
                    # must never be recast as an evidence-free no-signal decision.
                    continue
                accepted = set()
                for review_result in result.get("reviews") or []:
                    review_id = review_result.get("review_id")
                    if review_id not in requested_ids or review_id in accepted:
                        continue
                    source = cleaned_by_id.get(review_id)
                    if not source:
                        continue
                    status = review_result.get("status")
                    rows = review_result.get("events") or []
                    if status == "已生成事件" and rows:
                        event_rows = []
                        for ordinal, event in enumerate(rows, 1):
                            item = dict(event)
                            item["event_id"] = "%s-事%02d" % (review_id, ordinal)
                            item["review_id"] = review_id
                            item["asin"] = source.get("asin", "")
                            item["rating"] = source.get("rating", "")
                            item["review_date"] = source.get("review_date", "")
                            item["review_text"] = source.get("content", "")
                            event_rows.append(item)
                        known_event_ids = {event.get("event_id") for event in events}
                        if any(event["event_id"] in known_event_ids for event in event_rows):
                            continue
                        events.extend(event_rows)
                        event_counts[review_id] = len(event_rows)
                        processing_row = {"review_id": review_id, "status": "已生成事件",
                                          "reason": "缺失评论的定向补抽完成",
                                          "event_count": len(event_rows)}
                    elif status == "无可用需求信号" and not rows and str(review_result.get("no_signal_reason") or "").strip():
                        processing_row = {"review_id": review_id, "status": "无可用需求信号",
                                          "reason": str(review_result.get("no_signal_reason"))[:500],
                                          "event_count": 0}
                        event_counts[review_id] = 0
                    else:
                        continue
                    if review_id in processing_by_id:
                        processing_by_id[review_id].update(processing_row)
                    else:
                        processing.append(processing_row)
                        processing_by_id[review_id] = processing_row
                    accepted.add(review_id)
            events_doc["events"] = events
            processing_doc["reviews"] = processing
            _write(cache / "events.json", events_doc)
            _write(cache / "review_processing.json", processing_doc)
            pending = pending_reviews()

        if pending:
            pending_ids = {row.get("review_id") for row in pending}
            for review in pending:
                review_id = review.get("review_id")
                failed = {"review_id": review_id, "status": "失败",
                          "reason": "抽取模型未返回可验证的处理结果，未将其视为无需求信号",
                          "event_count": event_counts.get(review_id, 0)}
                if review_id in processing_by_id:
                    processing_by_id[review_id].update(failed)
                else:
                    processing.append(failed)
                    processing_by_id[review_id] = failed
            processing_doc["reviews"] = processing
            _write(cache / "review_processing.json", processing_doc)
            self._advance_coverage_round(workdir, "extraction")
            raise ValueError("抽取覆盖仍缺少 %s 条评论的有效结果" % len(pending_ids))

        events_doc["events"] = events
        processing_doc["reviews"] = processing
        _write(cache / "events.json", events_doc)
        _write(cache / "review_processing.json", processing_doc)

    @staticmethod
    def _review_selection(events):
        selected = {e["event_id"] for e in events if e.get("confidence") == "低" or
                    ((e.get("motivation") or {}).get("inferred") and not (e.get("motivation") or {}).get("inference_basis")) or
                    not e.get("evidence_quote") or
                    _normal_text(e.get("evidence_quote")) not in _normal_text(e.get("review_text"))}
        strata = {}
        for event in events:
            strata.setdefault((event.get("asin"), event.get("rating")), []).append(event)
        for rows in strata.values():
            rows = sorted(rows, key=lambda e: _hash(e.get("event_id")))
            selected.update(e["event_id"] for e in rows[:max(1, int(math.ceil(len(rows) * .10)))])
        return selected

    @staticmethod
    def _review_cache_complete(workdir):
        cache = Path(workdir) / "cache"
        review = _read(cache / "semantic_review.json", {}) or {}
        selected = set(review.get("selected_event_ids") or [])
        decided = {row.get("event_id") for row in review.get("decisions") or []}
        return (bool(review.get("complete")) and selected <= decided and
                not review.get("missing_event_ids") and not review.get("unresolved_quote_event_ids"))

    def _review(self, run, revision, workdir):
        path = Path(workdir) / "cache" / "events.json"
        doc = _read(path); events = doc["events"]
        review_path = Path(workdir) / "cache" / "semantic_review.json"
        previous = _read(review_path, {}) or {}
        coverage_round = self._coverage_round(workdir, "review")
        dropped_unverifiable_event_ids = set(previous.get("dropped_unverifiable_event_ids") or [])
        event_review_ids = {event.get("event_id"): event.get("review_id") for event in events}
        _, unresolved_before = _repair_event_quotes(events)
        # After two focused reviewer rounds, do not keep spending calls on an event whose
        # quote still cannot be verified verbatim. Exclude it from all downstream claims.
        if coverage_round >= 2:
            dropped_unverifiable_event_ids.update(unresolved_before)
            events = [event for event in events if event.get("event_id") not in unresolved_before]
            doc["events"] = events
        selected = self._review_selection(events)
        review_rows = [e for e in events if e["event_id"] in selected]
        current_event_ids = {event["event_id"] for event in events}
        current_selection_rule = "低置信/无依据推断/无效原文证据 + ASIN×评分10%分层抽样；空缺的可选字段不单独触发复核"
        reuse_previous = str(previous.get("selection_rule", "")).startswith(
            "低置信/无依据推断/无效原文证据")
        decisions_by_id = ({row.get("event_id"): row for row in previous.get("decisions") or []
                            if row.get("event_id") in selected and row.get("event_id") in current_event_ids}
                           if reuse_previous else {})
        # Re-open unresolved quotes even if an earlier reviewer accepted them.
        for event_id in unresolved_before:
            decisions_by_id.pop(event_id, None)
        missing_event_ids = []
        call_chunks = []
        for batch_number, chunk in enumerate(_chunks(review_rows, REVIEW_EVENTS_PER_CALL), 1):
            pending = [event for event in chunk if event["event_id"] not in decisions_by_id]
            base_batch_id = "review-r%02d-%05d" % (coverage_round, batch_number)
            if pending:
                requested_ids = sorted(event["event_id"] for event in pending)
                payload = {"events": pending,
                           "selection_reason": "低置信/异常 + ASIN×评分10%分层抽样；每个 required_event_id 必须返回一次复核决定",
                           "required_event_ids": requested_ids}
                call_chunks.append((base_batch_id, pending, payload))
        base_results = self._call_many(run, revision, workdir, "review_events",
                                       [(batch_id, payload) for batch_id, _, payload in call_chunks], reviewer=True)
        for (base_batch_id, original_chunk, _), result in zip(call_chunks, base_results):
            pending = list(original_chunk)
            for attempt in range(3):
                if not pending:
                    break
                requested_ids = {event["event_id"] for event in pending}
                if attempt:
                    result = self._call(
                        run, revision, workdir, "review_events", base_batch_id + "-coverage-%02d" % attempt,
                        {"events": pending,
                         "selection_reason": "只补齐 required_event_ids 中遗漏的复核决定",
                         "required_event_ids": sorted(requested_ids)}, reviewer=True)
                for decision in result.get("decisions") or []:
                    event_id = decision.get("event_id")
                    if event_id in requested_ids and event_id not in decisions_by_id:
                        decisions_by_id[event_id] = decision
                pending = [event for event in pending if event["event_id"] not in decisions_by_id]
            missing_event_ids.extend(event["event_id"] for event in pending)

        decisions = [decisions_by_id[event["event_id"]] for event in review_rows
                     if event["event_id"] in decisions_by_id]
        review_doc = {
            "schema": "amazon-voc-semantic-review/v1",
            "selected_event_ids": sorted(selected),
            "selection_rule": current_selection_rule,
            "decisions": decisions,
            "missing_event_ids": sorted(set(missing_event_ids)),
            "unresolved_quote_event_ids": [],
            "complete": not missing_event_ids,
            "coverage_round": coverage_round,
            "review_model": self.settings.reviewer_model,
            "generated_at": utcnow(),
        }
        if missing_event_ids:
            _write(review_path, review_doc)
            self._advance_coverage_round(workdir, "review")
            raise ValueError("语义复核仍缺少 %s 个事件的决定" % len(set(missing_event_ids)))

        by_id = {e["event_id"]: e for e in events}
        for decision in decisions:
            event_id = decision.get("event_id")
            if event_id not in by_id:
                continue
            if decision.get("action") == "drop":
                by_id.pop(event_id, None)
            elif decision.get("action") == "replace" and decision.get("replacement"):
                before = by_id[event_id]
                replacement = dict(decision["replacement"])
                for key in ("event_id", "review_id", "asin", "rating", "review_date", "review_text"):
                    replacement[key] = before.get(key)
                by_id[event_id] = replacement
            else:
                by_id[event_id]["review_status"] = "reviewed"
        doc["events"] = list(by_id.values())
        repaired_after, unresolved_after = _repair_event_quotes(doc["events"])
        unresolved_after = sorted(set(unresolved_after))
        if unresolved_after and coverage_round >= 2:
            dropped_unverifiable_event_ids.update(unresolved_after)
            unresolved_ids = set(unresolved_after)
            doc["events"] = [event for event in doc["events"] if event.get("event_id") not in unresolved_ids]
            unresolved_after = []
        review_doc["unresolved_quote_event_ids"] = unresolved_after
        review_doc["complete"] = not missing_event_ids and not unresolved_after
        review_doc["dropped_unverifiable_event_ids"] = sorted(dropped_unverifiable_event_ids)
        review_doc["dropped_unverifiable_event_count"] = len(dropped_unverifiable_event_ids)
        review_doc["dropped_unverifiable_reason"] = (
            "引文经过两轮定向复核仍无法在原评论中精确定位，相关事件已排除"
            if dropped_unverifiable_event_ids else ""
        )
        if repaired_after:
            review_doc["evidence_quotes_repaired"] = repaired_after
        doc["metadata"]["review_model"] = self.settings.reviewer_model
        _write(path, doc)
        _write(review_path, review_doc)
        processing_path = Path(workdir) / "cache" / "review_processing.json"
        processing_doc = _read(processing_path)
        counts = {}
        for event in doc["events"]:
            counts[event["review_id"]] = counts.get(event["review_id"], 0) + 1
        for row in processing_doc["reviews"]:
            row["event_count"] = counts.get(row["review_id"], 0)
            if not row["event_count"] and row["status"] == "已生成事件":
                excluded_for_quote = any(
                    event_id in dropped_unverifiable_event_ids and event_review_ids.get(event_id) == row["review_id"]
                    for event_id in dropped_unverifiable_event_ids
                )
                reason = "引文无法精确定位到评论原文，相关事件已排除" if excluded_for_quote else "复核后无合格事件"
                row["status"], row["reason"] = "无可用需求信号", reason
        _write(processing_path, processing_doc)
        if unresolved_after:
            self._advance_coverage_round(workdir, "review")
            review_doc["coverage_round"] = self._coverage_round(workdir, "review")
            _write(review_path, review_doc)
            raise ValueError("有 %s 个事件的复核引用仍无法精确定位到评论原文" % len(unresolved_after))

    @staticmethod
    def _withhold_unbounded_inferences(workdir):
        cache = Path(workdir) / "cache"
        events_path = cache / "events.json"
        events_doc = _read(events_path, {}) or {}
        withheld = []
        for event in events_doc.get("events") or []:
            motivation = event.get("motivation") or {}
            if motivation.get("inferred") and not (motivation.get("inference_basis") or []):
                motivation["inferred"] = ""
                motivation["inference_basis"] = []
                event["motivation"] = motivation
                if event.get("event_id"):
                    withheld.append(event["event_id"])
        if not withheld:
            return 0
        _write(events_path, events_doc)
        review_path = cache / "semantic_review.json"
        review_doc = _read(review_path, {}) or {}
        prior = set(review_doc.get("unbounded_inference_withheld_event_ids") or [])
        prior.update(withheld)
        review_doc["unbounded_inference_withheld_event_ids"] = sorted(prior)
        review_doc["unbounded_inference_withheld_count"] = len(prior)
        review_doc["unbounded_inference_withheld_reason"] = (
            "复核后仍缺少可核验推理依据，已撤下推断字段；事件中有证据支持的其他内容保留。"
        )
        _write(review_path, review_doc)
        return len(withheld)

    def _normalize(self, run, revision, workdir):
        events = _read(Path(workdir) / "cache" / "events.json")["events"]
        master = _read(Path(workdir) / "cache" / "master.json")
        dictionary_path = Path(workdir) / "cache" / "tag_dictionary.json"
        prior_dictionary = _read(dictionary_path, {}) or {}
        dictionary_payload = {"events": project_model_events(events), "namespaces": NAMESPACES,
                              "marketplace": master.get("marketplace"), "category": master.get("category")}
        dictionary_input_bytes = _json_size_bytes(dictionary_payload)
        event_by_id = {e["event_id"]: e for e in events}
        dictionary_batches, oversized_dictionary_event_ids = build_dictionary_batches(
            events, marketplace=master.get("marketplace"), category=master.get("category"))
        dictionary_results = self._call_many(
            run, revision, workdir, "build_dictionary",
            [("dictionary-r00-%05d" % batch_number, batch["payload"])
             for batch_number, batch in enumerate(dictionary_batches, 1)])
        tags = merge_dictionary_batch_results(dictionary_batches, dictionary_results, event_by_id)
        tag_by_id = {tag.get("tag_id"): tag for tag in tags}
        valid_mappings = {}
        repair_round = self._coverage_round(workdir, "mapping")
        prior_metadata = prior_dictionary.get("metadata") or {}
        prior_model_unmapped = int(prior_metadata.get(
            "mapping_model_unmapped_count", prior_metadata.get("mapping_missing_count", 0)) or 0)
        mapping_candidates = build_mapping_candidates(events)
        candidate_by_id = {row["candidate_id"]: row for row in mapping_candidates}
        expected_candidate_ids = set(candidate_by_id)
        mapping_candidate_digest = _hash(sorted(expected_candidate_ids))
        previous_candidate_digest = prior_metadata.get("mapping_candidate_digest")
        model_candidates, oversized_mapping_candidate_ids = [], set()
        for candidate in mapping_candidates:
            try:
                build_mapping_candidate_batches([candidate], tags)
            except ValueError:
                oversized_mapping_candidate_ids.add(candidate["candidate_id"])
            else:
                model_candidates.append(candidate)
        model_candidate_ids = {row["candidate_id"] for row in model_candidates}
        mapping_batches = build_mapping_candidate_batches(model_candidates, tags)
        mapping_chunks = []
        for batch_number, batch in enumerate(mapping_batches, 1):
            candidate_ids = {row["candidate_id"] for row in batch["candidates"]}
            batch_id = "mapping-r00-%05d" % batch_number
            mapping_chunks.append((batch_id, candidate_ids, batch["payload"]))
        base_results = (self._call_many(run, revision, workdir, "map_dictionary",
                                        [(batch_id, payload) for batch_id, _, payload in mapping_chunks])
                        if mapping_chunks else [])

        def accept_valid(rows, allowed_ids):
            row_counts = {}
            for row in rows:
                candidate_id = row.get("candidate_id")
                row_counts[candidate_id] = row_counts.get(candidate_id, 0) + 1
            for row in rows:
                candidate_id = row.get("candidate_id")
                candidate = candidate_by_id.get(candidate_id)
                tag = tag_by_id.get(row.get("tag_id"))
                if (row_counts.get(candidate_id) == 1 and candidate_id in allowed_ids and candidate and tag and
                        tag.get("namespace") == candidate.get("namespace")):
                    valid_mappings.setdefault(candidate_id, {
                        "tag_id": row["tag_id"], "mapping_method": "model_normalized"})

        for (_, allowed_ids, _), result in zip(mapping_chunks, base_results):
            accept_valid(result.get("mappings") or [], allowed_ids)

        missing_ids = model_candidate_ids - set(valid_mappings)
        # Retry only unresolved unique values, never every event occurrence.
        same_prior_mapping_input = previous_candidate_digest == mapping_candidate_digest
        repair_attempts = 0 if prior_model_unmapped and same_prior_mapping_input else 2
        for repair_number in range(1, repair_attempts + 1):
            if not missing_ids:
                break
            missing_candidates = [candidate_by_id[candidate_id] for candidate_id in sorted(missing_ids)]
            repair_batches = build_mapping_candidate_batches(
                missing_candidates, tags, max_candidates=MAPPING_REPAIR_VALUES_PER_CALL)
            repair_jobs = []
            for batch_number, batch in enumerate(repair_batches, 1):
                candidate_ids = {row["candidate_id"] for row in batch["candidates"]}
                payload = {**batch["payload"], "required_candidates": sorted(candidate_ids),
                           "selection_reason": "仅修复未获得有效标签的唯一字段值；每个 candidate_id 必须引用同命名空间的有效 tag_id。"}
                batch_id = "mapping-r%02d-validation-%02d-%05d" % (
                    repair_round, repair_number, batch_number)
                repair_jobs.append((batch_id, candidate_ids, payload))
            repair_results = self._call_many(run, revision, workdir, "map_dictionary",
                                             [(batch_id, payload) for batch_id, _, payload in repair_jobs])
            for (_, allowed_ids, _), result in zip(repair_jobs, repair_results):
                accept_valid(result.get("mappings") or [], allowed_ids)
            missing_ids = model_candidate_ids - set(valid_mappings)

        missing_mapping_count = len(model_candidate_ids - set(valid_mappings))
        raw_fallback_count = 0
        raw_fallback_candidate_ids = expected_candidate_ids - set(valid_mappings)
        if raw_fallback_candidate_ids:
            # Preserve uncategorized values as raw tags; expand the stable ID back
            # to every event-field occurrence locally after mapping.
            fallback_groups = {}
            for candidate_id in sorted(raw_fallback_candidate_ids):
                candidate = candidate_by_id[candidate_id]
                group_key = (candidate["namespace"], _normal_text(candidate["raw_value"]))
                group = fallback_groups.setdefault(group_key, {
                    "candidate_ids": [], "raw_values": [], "event_ids": set()})
                group["candidate_ids"].append(candidate_id)
                if candidate["raw_value"] not in group["raw_values"]:
                    group["raw_values"].append(candidate["raw_value"])
                group["event_ids"].update(row["event_id"] for row in candidate["event_rows"])
            used_tag_ids = {tag.get("tag_id") for tag in tags}
            for (namespace, normalized_raw), group in sorted(fallback_groups.items()):
                raw_value = sorted(group["raw_values"], key=lambda value: (len(value), value))[0]
                tag_id = _stable("RAW", namespace, normalized_raw)
                if tag_id in used_tag_ids:
                    tag_id = _stable("RAW", namespace, normalized_raw, "passthrough")
                used_tag_ids.add(tag_id)
                support_ids = sorted(group["event_ids"])
                review_count = len({event_by_id[event_id]["review_id"] for event_id in support_ids})
                tags.append({"tag_id": tag_id, "namespace": namespace, "canonical_name": raw_value,
                             "definition": "原始字段值透传，未执行同义词归并；不代表已确认的标准术语。",
                             "parent_tag_id": "", "raw_variants": sorted(group["raw_values"]),
                             "include_examples": [], "exclude_examples": [],
                             "support_event_ids": support_ids, "independent_review_count": review_count,
                             "confidence": "原始表达透传", "status": "exploratory",
                             "normalization_status": "raw_unmerged"})
                for candidate_id in group["candidate_ids"]:
                    valid_mappings[candidate_id] = {"tag_id": tag_id,
                                                    "mapping_method": "raw_passthrough"}

        mappings = []
        for candidate_id, mapping in sorted(valid_mappings.items()):
            candidate = candidate_by_id[candidate_id]
            for row in candidate["event_rows"]:
                mappings.append({**row, "tag_id": mapping["tag_id"],
                                 "mapping_method": mapping["mapping_method"]})
                if mapping["mapping_method"] == "raw_passthrough":
                    raw_fallback_count += 1
        unresolved_mapping_count = len(expected_candidate_ids - set(valid_mappings))
        if unresolved_mapping_count:
            self._advance_coverage_round(workdir, "mapping")
        _write(dictionary_path,
               {"schema": "amazon-voc-tag-dictionary/v1", "metadata": {"prompt_version": self.prompts.load("build_dictionary")[0]["version"],
                  "model": self.settings.extractor_model, "generated_at": (prior_dictionary.get("metadata") or {}).get("generated_at") or utcnow(),
                  "mapping_coverage_round": repair_round, "mapping_missing_count": unresolved_mapping_count,
                  "mapping_model_unmapped_count": missing_mapping_count,
                  "normalized_tag_count": sum(tag.get("normalization_status") != "raw_unmerged" for tag in tags),
                  "raw_unmerged_tag_count": sum(tag.get("normalization_status") == "raw_unmerged" for tag in tags),
                  "raw_passthrough_mapping_count": raw_fallback_count,
                  "dictionary_generation_mode": ("model_normalized" if len(dictionary_batches) == 1
                                                 else "model_batched" if dictionary_batches
                                                 else "no_model_input_oversized_event_fallback"),
                  "dictionary_input_bytes": dictionary_input_bytes,
                  "dictionary_batch_count": len(dictionary_batches),
                  "dictionary_oversized_event_count": len(oversized_dictionary_event_ids),
                  "mapping_oversized_candidate_count": len(oversized_mapping_candidate_ids),
                  "mapping_candidate_count": len(mapping_candidates),
                  "mapping_candidate_digest": mapping_candidate_digest}, "tags": tags,
                  "mappings": mappings})

    def _segment(self, run, revision, workdir):
        cache = Path(workdir) / "cache"
        events = _read(cache / "events.json")["events"]
        dictionary = _read(cache / "tag_dictionary.json")
        cleaned = _read(cache / "cleaned_reviews.json")["rows"]
        master = _read(cache / "master.json")
        config = master.get("analysis_config") or {}
        minimum = max(int(config.get("segment_min_review_count", 2)),
                      int(math.ceil(len(cleaned) * float(config.get("segment_min_review_rate", .005)))))
        proposal_input = build_segment_proposal_input(events, dictionary, minimum)
        proposal_batches = build_segment_proposal_batches(proposal_input)
        proposal_results = self._call_many(
            run, revision, workdir, "propose_segments",
            [("segments" if len(proposal_batches) == 1 else "segments-b%04d" % index, batch["payload"])
             for index, batch in enumerate(proposal_batches, 1)])
        segments = []
        seen_segment_keys = set()
        for proposals in proposal_results:
            for proposed in proposals.get("segments") or []:
                segment = dict(proposed)
                segment_key = segment.get("segment_key") or {}
                stable_key = tuple(_normal_text(segment_key.get(key))
                                   for key in ("audience", "scenario", "motivation", "task"))
                if not any(stable_key) or stable_key in seen_segment_keys:
                    continue
                seen_segment_keys.add(stable_key)
                segments.append(segment)
        if not segments:
            raise ValueError("细分建议没有返回可用的人群、场景、动机或任务候选")
        used = set()
        for segment in segments:
            segment_key = segment.get("segment_key")
            if not isinstance(segment_key, dict):
                segment_key = {}
            else:
                segment_key = dict(segment_key)
            for dimension in ("audience", "scenario", "motivation", "task"):
                value = segment_key.get(dimension)
                if not isinstance(value, str) or not value.strip():
                    # An explicit unknown preserves the distinction between absent evidence
                    # and an invented audience, scenario, motivation, or task.
                    segment_key[dimension] = "未明确"
            segment["segment_key"] = segment_key
            segment["segment_id"] = segment.get("segment_id") or _stable("SEG", *(segment.get("segment_key") or {}).values())
            if segment["segment_id"] in used:
                segment["segment_id"] = _stable("SEG", segment["segment_id"], segment.get("name"))
            used.add(segment["segment_id"]); segment["member_event_ids"] = []
        assignments_by_id = {}
        coverage_round = self._coverage_round(workdir, "assignment")
        assignment_chunks = []
        for batch_number, chunk in enumerate(_chunks(events, ASSIGNMENT_EVENTS_PER_CALL), 1):
            # Keep deterministic base IDs stable across pipeline retries. Input
            # hashes already invalidate changed payloads; bumping this ID after
            # a coverage failure would reissue every completed base assignment.
            batch_id = "assignment-r00-%05d" % batch_number
            payload = {"events": project_model_events(chunk), "segments": segments,
                       "required_event_ids": sorted(event["event_id"] for event in chunk)}
            assignment_chunks.append((batch_id, chunk, payload))
        base_results = self._call_many(run, revision, workdir, "assign_segments",
                                       [(batch_id, payload) for batch_id, _, payload in assignment_chunks])
        for (base_batch_id, chunk, _), assignment in zip(assignment_chunks, base_results):
            pending = list(chunk)
            for attempt in range(4):
                if not pending:
                    break
                requested_ids = {event["event_id"] for event in pending}
                if attempt:
                    if attempt <= 2:
                        batch_id = base_batch_id + "-coverage-%02d" % attempt
                    else:
                        batch_id = base_batch_id + "-coverage-r%02d-%02d" % (coverage_round, attempt)
                    repair_schema = json.loads(json.dumps(SCHEMAS["assign_segments"]))
                    repair_schema["properties"]["assignments"]["items"]["properties"]["segment_id"]["enum"] = sorted(used)
                    assignment = self._call(run, revision, workdir, "assign_segments", batch_id,
                                            {"events": project_model_events(pending), "segments": segments,
                                             "required_event_ids": sorted(requested_ids)},
                                            schema_override=repair_schema)
                for row in assignment.get("assignments") or []:
                    event_id = row.get("event_id")
                    if event_id in requested_ids and row.get("segment_id") in used:
                        assignments_by_id.setdefault(event_id, row)
                pending = [event for event in pending if event["event_id"] not in assignments_by_id]
            if pending:
                self._advance_coverage_round(workdir, "assignment")
                raise ValueError("细分归属仍缺少 %s 个事件" % len(pending))
        by_segment = {s["segment_id"]: s for s in segments}
        for row in assignments_by_id.values():
            if row.get("segment_id") in by_segment:
                by_segment[row["segment_id"]]["member_event_ids"].append(row.get("event_id"))
        event_by_id = {e["event_id"]: e for e in events}
        for segment in segments:
            reviews = {event_by_id[eid]["review_id"] for eid in segment["member_event_ids"] if eid in event_by_id}
            segment["status"] = "formal" if len(reviews) >= minimum else "exploratory"
            segment["evidence_event_ids"] = segment["member_event_ids"][:20]
        proposed_segment_count = len(segments)
        inactive_segments = [segment for segment in segments if not segment["member_event_ids"]]
        segments = [segment for segment in segments if segment["member_event_ids"]]
        if not segments:
            raise ValueError("细分归属后没有包含样本事件的有效细分")
        metadata = {"model": self.settings.extractor_model, "model_version": self.settings.extractor_model,
                    "prompt_version": self.prompts.load("propose_segments")[0]["version"], "generated_at": utcnow(),
                    "proposal_mode": "profile_batches" if len(proposal_batches) > 1 else "single_profile_batch",
                    "proposal_batch_count": len(proposal_batches),
                    "proposal_profile_count": len(proposal_input.get("profiles") or []),
                    "proposed_segment_count": proposed_segment_count,
                    "inactive_candidate_count": len(inactive_segments),
                    "proposal_event_count": len(events),
                    "proposal_input_bytes": _json_size_bytes(proposal_input),
                    "proposal_max_batch_bytes": max((batch["input_bytes"] for batch in proposal_batches), default=0)}
        _write(cache / "segment_model.json", {"schema": "amazon-voc-segment-model/v3", "metadata": metadata,
                                               "segments": segments, "journey": {}})

    def _journey(self, run, revision, workdir):
        cache = Path(workdir) / "cache"
        events = _read(cache / "events.json")["events"]
        doc = _read(cache / "segment_model.json")
        segment_by_event = {event_id: segment["segment_id"]
                            for segment in doc["segments"]
                            for event_id in segment.get("member_event_ids") or []}
        stage_proposal = build_journey_stage_proposal_input(events, doc["segments"])
        stage_payload = {**stage_proposal, "event_assignments": [], "fixed_stages": []}
        stages_result = self._call(
            run, revision, workdir, "build_journey", "journey-stages",
            stage_payload)
        stages = stages_result.get("stages") or []
        if not stages:
            raise ValueError("旅程阶段目录为空")
        assignments = []
        coverage_round = self._coverage_round(workdir, "journey")
        journey_chunks = []
        for batch_number, chunk in enumerate(_chunks(events, JOURNEY_EVENTS_PER_CALL), 1):
            chunk_ids = {event.get("event_id") for event in chunk}
            chunk_segment_ids = {segment_by_event[event_id] for event_id in chunk_ids
                                 if event_id in segment_by_event}
            chunk_segments = [{key: segment.get(key, "") for key in
                               ("segment_id", "name", "segment_key", "persona_summary", "jtbd_statement")}
                              for segment in doc["segments"] if segment["segment_id"] in chunk_segment_ids]
            chunk_memberships = [{"event_id": event_id, "segment_id": segment_id}
                                 for event_id, segment_id in segment_by_event.items()
                                 if event_id in chunk_ids]
            batch_id = "journey-map-r%02d-%05d" % (coverage_round, batch_number)
            payload = {"events": project_model_events(chunk), "segments": chunk_segments,
                       "event_assignments": chunk_memberships, "fixed_stages": stages,
                       "required_event_ids": sorted(chunk_ids)}
            journey_chunks.append((batch_id, chunk, chunk_ids, chunk_segment_ids, chunk_memberships,
                                   chunk_segments, payload))
        base_results = self._call_many(run, revision, workdir, "build_journey",
                                       [(batch_id, payload) for batch_id, _, _, _, _, _, payload in journey_chunks])
        for (base_batch_id, chunk, chunk_ids, chunk_segment_ids, chunk_memberships, chunk_segments, _), result in zip(journey_chunks, base_results):
            assigned_ids = set()
            pending_ids = set(chunk_ids)
            for row in result.get("assignments") or []:
                if (row.get("event_id") in pending_ids and row.get("event_id") not in assigned_ids and
                        row.get("stage_id") in {s["stage_id"] for s in stages}):
                    assignments.append(row)
                    assigned_ids.add(row["event_id"])
            pending_ids -= assigned_ids
            for attempt in range(3):
                if not pending_ids:
                    break
                pending_events = [event for event in chunk if event.get("event_id") in pending_ids]
                pending_memberships = [row for row in chunk_memberships if row["event_id"] in pending_ids]
                batch_id = base_batch_id + "-coverage-%02d" % (attempt + 1)
                result = self._call(
                    run, revision, workdir, "build_journey", batch_id,
                    {"events": project_model_events(pending_events), "segments": chunk_segments,
                     "event_assignments": pending_memberships, "fixed_stages": stages,
                     "required_event_ids": sorted(pending_ids)})
                for row in result.get("assignments") or []:
                    if row.get("event_id") in pending_ids and row.get("stage_id") in {s["stage_id"] for s in stages}:
                        if row["event_id"] not in assigned_ids:
                            assignments.append(row)
                            assigned_ids.add(row["event_id"])
                pending_ids = chunk_ids - assigned_ids
            if assigned_ids != chunk_ids:
                self._advance_coverage_round(workdir, "journey")
                raise ValueError("旅程阶段归属仍缺少 %s 个事件" % len(chunk_ids - assigned_ids))
        doc["journey"] = {"stages": stages,
                          "stage_generation": {
                              "method": stage_proposal["sampling_method"],
                              "event_count": stage_proposal["event_count"],
                              "sampled_event_count": stage_proposal["sampling"]["sampled_event_count"],
                              "active_segment_count": stage_proposal["sampling"]["active_segment_count"],
                              "max_samples_per_segment": stage_proposal["sampling"]["max_samples_per_segment"],
                              "input_bytes": _json_size_bytes(stage_payload),
                          },
                          "event_assignments": [{"event_id": row.get("event_id"), "stage_id": row.get("stage_id")}
                                                for row in assignments]}
        _write(cache / "segment_model.json", doc)

    def _compile(self, run, revision, workdir):
        report = evaluate(Path(workdir), include_outputs=False)
        if not report["precompile_green"]:
            raise ValueError("质量门禁未通过，已停止报告生成")
        self._run_script("compile_analysis.py", "--workdir", workdir)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        report = evaluate(Path(workdir), include_outputs=True)
        if not report["all_green"]:
            raise ValueError("输出一致性门禁未通过")
        analysis_path = Path(workdir) / "cache" / "analysis.json"
        analysis = _read(analysis_path); analysis["quality"] = report; _write(analysis_path, analysis)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        self._run_script("validate.py", "--workdir", workdir)
        for kind in ("html", "excel", "analysis", "quality", "calls"):
            path = self.storage.artifact(run["project_id"], run["id"], revision, kind)
            if path:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.db.execute("INSERT INTO artifacts(run_id,revision,kind,path,sha256,created_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(run_id,revision,kind) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,created_at=excluded.created_at",
                                (run["id"], revision, kind, str(path), digest, utcnow()))

    def _compile_full_voc_v2(self, run, revision, workdir):
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        self._run_script("validate_full_voc_v2.py", "--workdir", workdir)
        analysis_path = Path(workdir) / "cache" / "analysis.json"
        analysis = _read(analysis_path, {})
        quality = _read(Path(workdir) / "cache" / "quality_report.json", {})
        analysis["quality"] = quality
        analysis["validation"] = _read(Path(workdir) / "cache" / "voc_v2_delivery_report.json", {})
        _write(analysis_path, analysis)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        self._run_script("validate_full_voc_v2.py", "--workdir", workdir)
        report = _read(Path(workdir) / "cache" / "voc_v2_delivery_report.json", {})
        if not report.get("all_green"):
            raise ValueError("V2 报告输出一致性门禁未通过")
        # The final deterministic check is reflected in both downloadable outputs.
        quality = _read(Path(workdir) / "cache" / "quality_report.json", {})
        analysis = _read(analysis_path, {})
        analysis["quality"] = quality
        analysis["validation"] = report
        _write(analysis_path, analysis)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        final_report = self._run_script("validate_full_voc_v2.py", "--workdir", workdir)
        final_report = _read(Path(workdir) / "cache" / "voc_v2_delivery_report.json", {})
        if not final_report.get("all_green"):
            raise ValueError("V2 最终报告一致性门禁未通过")
        for kind in ("html", "excel", "analysis", "quality", "calls"):
            path = self.storage.artifact(run["project_id"], run["id"], revision, kind)
            if path:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.db.execute("INSERT INTO artifacts(run_id,revision,kind,path,sha256,created_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(run_id,revision,kind) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,created_at=excluded.created_at",
                                (run["id"], revision, kind, str(path), digest, utcnow()))

    def _process_full_voc_v2(self, run, revision, workdir):
        cache = Path(workdir) / "cache"
        if not (cache / "cleaned_reviews.json").exists():
            self._initialize(run, workdir)
        cleaned_rows = (_read(cache / "cleaned_reviews.json", {"rows": []}) or {}).get("rows") or []
        master = _read(cache / "master.json", {}) or {}
        master["analysis_mode"] = "full_voc_v2"
        payload = build_payload(master, cleaned_rows)
        self._status(run["id"], "extracting")

        chunks = list(_full_voc_v2_batches(payload["reviews"], self.settings.batch_reviews,
                                            self.settings.batch_chars))
        extraction_jobs = [
            ("chunk-%04d" % index, _full_voc_v2_extract_payload(rows, payload["task_settings"]))
            for index, rows in enumerate(chunks, 1)
        ]
        chunk_results = self._call_many(run, revision, workdir, "full_voc_v2_extract", extraction_jobs,
                                        fail_fast=True)
        extraction = {
            "review_processing": [row for result in chunk_results
                                  for row in result.get("review_processing", [])],
            "events": [row for result in chunk_results for row in result.get("events", [])],
        }
        extraction_errors = validate_extract_result(
            _full_voc_v2_extract_payload(payload["reviews"], payload["task_settings"]), extraction)
        if extraction_errors:
            raise ValueError("V2 分块抽取覆盖门禁未通过：" + "；".join(extraction_errors[:12]))
        extraction = assign_stable_event_ids(extraction)
        _write(cache / "full_voc_v2_extraction.json", extraction)

        synthesis_payload = build_synthesis_payload(payload, extraction["events"])
        review_context = (run.get("sources") or {}).get("review_source_context") or {}
        if review_context:
            synthesis_payload["task_settings"]["review_collection_scope"] = {
                "requested_asin_count": int(review_context.get("requested_asin_count") or 0),
                "completed_asin_count": int(review_context.get("completed_asin_count") or 0),
                "failed_asins": list(review_context.get("failed_asins") or []),
                "exported_review_count": int(review_context.get("review_count") or 0),
            }
        if extraction["events"]:
            semantic_result = self._call(run, revision, workdir, "full_voc_v2_synthesis", "global", synthesis_payload)
            dictionary_candidates = build_dictionary_candidates(synthesis_payload["events"])
            dictionary_result = {"groups": []}
            dictionary_status = ("not_needed" if dictionary_candidates["mergeable_candidate_count"] < 2
                                else "completed")
            dictionary_payload = {"candidates": dictionary_candidates["candidates"]}
            if dictionary_status == "completed":
                try:
                    dictionary_result = self._call(
                        run, revision, workdir, "full_voc_v2_dictionary", "high-frequency",
                        dictionary_payload,
                    )
                except Exception as exc:
                    # Dictionary enrichment is optional; preserve exact raw mappings if it fails.
                    dictionary_result = {"groups": []}
                    dictionary_status = "failed"
                    _write(cache / "v2_dictionary_normalization_error.json", {
                        "error_type": type(exc).__name__,
                    })
            dictionary_normalization = {
                "candidates": dictionary_candidates["candidates"],
                "candidate_coverage": dictionary_candidates["candidate_coverage"],
                "result": dictionary_result,
            }
            _write(cache / "v2_dictionary_candidates.json", dictionary_candidates)
            _write(cache / "v2_dictionary_normalization.json", {
                "status": dictionary_status, **dictionary_result,
            })
            assignment_jobs = build_assignment_jobs(synthesis_payload, semantic_result,
                                                    ASSIGNMENT_EVENTS_PER_CALL)
            assignment_results = self._call_many(run, revision, workdir, "full_voc_v2_assign",
                                                  assignment_jobs, fail_fast=True)
            combined_synthesis = dict(semantic_result)
            combined_synthesis["event_assignments"] = [
                row for assignment_result in assignment_results
                for row in assignment_result.get("event_assignments", [])
            ]
            synthesis, repair_summary = repair_synthesis_contract(
                synthesis_payload, combined_synthesis,
                dictionary_normalization=dictionary_normalization,
                dictionary_status=dictionary_status,
            )
            repair_summary.update({
                "dictionary_candidate_coverage": dictionary_candidates["candidate_coverage"],
                "dictionary_unique_values": dictionary_candidates["total_unique_values"],
                "dictionary_candidate_review_value_occurrences": dictionary_candidates[
                    "candidate_review_value_occurrences"],
            })
            assignment_repairs = {"missing": 0, "duplicate": 0, "invalid_path": 0, "unknown": 0}
            for batch_id, _ in assignment_jobs:
                repair_path = cache / "batches" / "full_voc_v2_assign" / (batch_id + ".repairs.json")
                batch_summary = _read(repair_path, {}) or {}
                for key in assignment_repairs:
                    assignment_repairs[key] += int(batch_summary.get(key) or 0)
            repair_summary.update({"assignment_batch_" + key: value
                                   for key, value in assignment_repairs.items()})
            if any(repair_summary.values()):
                _write(cache / "v2_synthesis_repairs.json", repair_summary)
        else:
            semantic_result = _empty_full_voc_v2_synthesis(payload["sample_count"])
            synthesis = semantic_result
        result = assemble_full_result(payload, extraction, synthesis)
        collection_scope = payload.get("task_settings", {}).get("review_collection_scope") or {}
        failed_asins = list(collection_scope.get("failed_asins") or [])
        if failed_asins:
            requested = collection_scope.get("requested_asin_count") or "未知数量"
            completed = collection_scope.get("completed_asin_count") or 0
            exported_reviews = collection_scope.get("exported_review_count") or 0
            cleaned_reviews = payload.get("sample_count") or 0
            asin_list = ", ".join(failed_asins)
            result.setdefault("limitations", []).append({
                "zh": "评论采集范围：请求 %s 个 ASIN，本次 %s 个 ASIN 完成并导出 %s 条评论；清洗后分析 %s 条。失败 ASIN：%s。本报告不覆盖失败商品。" % (
                    requested, completed, exported_reviews, cleaned_reviews, asin_list),
                "en": "Review collection scope: %s ASINs were requested; %s completed and exported %s review rows, of which %s remained after cleaning. Failed ASINs: %s. This report does not cover the failed products." % (
                    requested, completed, exported_reviews, cleaned_reviews, asin_list),
            })
        result_errors = validate_result(payload, result)
        if result_errors:
            raise ValueError("V2 全局综合与事件合并门禁未通过：" + "；".join(result_errors[:12]))

        prompt_meta, _ = self.prompts.load("full_voc_v2_synthesis")
        adapted = adapt_result(master, cleaned_rows, result,
                               self.settings.extractor_model, prompt_meta.get("version", PROMPT_VERSION))
        _write(cache / "full_voc_v2_synthesis_result.json", semantic_result)
        _write(cache / "full_voc_v2_result.json", result)
        _write(cache / "events.json", adapted["events_doc"])
        _write(cache / "review_processing.json", adapted["processing_doc"])
        _write(cache / "tag_dictionary.json", adapted["dictionary_doc"])
        _write(cache / "segment_model.json", adapted["segment_model"])
        _write(cache / "evidence_index.json", adapted["evidence_index"])
        _write(cache / "quality_report.json", adapted["quality_report"])
        _write(cache / "analysis.json", adapted["analysis"])
        self._export_calls(run, revision, workdir)
        self._status(run["id"], "compiling")
        self._compile_full_voc_v2(run, revision, workdir)

    def process(self, run_id):
        run = self.db.get_run(run_id)
        if not run or run["status"] in TERMINAL:
            return
        revision = int(run["current_revision"])
        workdir = self.storage.revision_dir(run["project_id"], run_id, revision)
        try:
            cache = workdir / "cache"
            for legacy_name in ("raw_alexa.json", "cleaned_alexa.json", "external_enrichment.json",
                                "manual_external_sources.json", "recommendations.json"):
                legacy_path = cache / legacy_name
                if legacy_path.exists():
                    legacy_path.unlink()
            analysis_mode = str((run.get("sources") or {}).get("analysis_mode") or "multi_stage_v4")
            if analysis_mode == "full_voc_v2":
                self._process_full_voc_v2(run, revision, workdir)
                self.db.update_run(run_id, status="completed", completed_at=utcnow(), error="")
                return
            if not (workdir / "cache" / "cleaned_reviews.json").exists():
                self._initialize(run, workdir); self._status(run_id, "cleaned")
            if not (workdir / "cache" / "events.json").exists():
                self._status(run_id, "extracting"); self._extract(run, revision, workdir)
            self._status(run_id, "extracting"); self._reconcile_extraction_coverage(run, revision, workdir)
            if not self._review_cache_complete(workdir):
                self._status(run_id, "reviewing"); self._review(run, revision, workdir)
            self._withhold_unbounded_inferences(workdir)
            self._status(run_id, "normalizing"); self._normalize(run, revision, workdir)
            self._status(run_id, "segmenting"); self._segment(run, revision, workdir)
            self._status(run_id, "journey"); self._journey(run, revision, workdir)
            self._export_calls(run, revision, workdir)
            self._status(run_id, "compiling"); self._compile(run, revision, workdir)
            self.db.update_run(run_id, status="completed", completed_at=utcnow(), error="")
        except InterruptedError:
            return
        except Exception as exc:
            self._export_calls(run, revision, workdir)
            self.db.update_run(run_id, status="failed", error=str(exc)[:4000], completed_at=utcnow())
            raise
