from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "full_voc_v2_extract_test_module",
    ROOT / "backend/amazon_voc/runtime/voc_app/full_voc_v2_extract.py",
)
_module = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_module)
assign_stable_event_ids = _module.assign_stable_event_ids
normalize_extract_response = _module.normalize_extract_response
validate_extract_result = _module.validate_extract_result


def _payload():
    return {
        "sample_count": 2,
        "task_settings": {"category": "general product"},
        "reviews": [
            {"sample_id": "S-1", "review_text": "The latch holds well during travel."},
            {"sample_id": "S-2", "review_text": "Nothing to add."},
        ],
    }


def _event(sample_id="S-1"):
    return {
        "sample_id": sample_id,
        "primary_for_review": True,
        "audience": {"user": ""},
        "scenario": {"context": "during travel"},
        "motivation": {"explicit": "", "inferred": "", "inference_basis": ""},
        "task": {"core_job": "keep items secured", "desired_outcome": "items stay secured"},
        "experience": {"object": "latch", "result": "holds well", "pain": "", "itch": ""},
        "workaround": {"action": ""},
        "evidence_quote": "holds well during travel",
        "confidence": "high",
        "fact_inference_boundary": "The review directly states this result.",
    }


def _result():
    return {
        "review_processing": [
            {"sample_id": "S-1", "status": "processed_with_events", "reason": "One supported experience event.", "event_count": 1},
            {"sample_id": "S-2", "status": "no_need_signal", "reason": "No product experience signal is stated.", "event_count": 0},
        ],
        "events": [_event()],
    }


def test_complete_batch_has_exact_coverage_and_valid_event_contract():
    assert validate_extract_result(_payload(), _result()) == []


def test_missing_duplicate_and_unknown_reviews_are_rejected():
    missing = _result()
    missing["review_processing"] = missing["review_processing"][:1]
    assert any("missing review_processing sample_id: S-2" in error
               for error in validate_extract_result(_payload(), missing))

    duplicate = _result()
    duplicate["review_processing"].append(deepcopy(duplicate["review_processing"][0]))
    assert any("duplicate review_processing sample_id: S-1" in error
               for error in validate_extract_result(_payload(), duplicate))

    unknown = _result()
    unknown["review_processing"][0]["sample_id"] = "S-404"
    assert any("unknown review_processing sample_id: S-404" in error
               for error in validate_extract_result(_payload(), unknown))
    unknown_event = _result()
    unknown_event["events"][0]["sample_id"] = "S-404"
    assert any("event references unknown sample_id: S-404" in error
               for error in validate_extract_result(_payload(), unknown_event))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda result: result["events"][0].update(evidence_quote="a made up phrase"), "not a verbatim source substring"),
        (lambda result: result["review_processing"][0].update(event_count=3), "event_count does not match"),
        (lambda result: result["review_processing"][0].update(status="no_need_signal"), "status must be processed_with_events"),
        (lambda result: result["events"][0].update(primary_for_review=False), "exactly one primary event"),
        (lambda result: result["events"][0].update(event_id="model-made-id"), "must not contain event_id"),
    ],
)
def test_invalid_quote_count_status_and_primary_are_rejected(mutate, message):
    result = _result()
    mutate(result)
    assert any(message in error for error in validate_extract_result(_payload(), result))


def test_multiple_events_require_exactly_one_primary():
    result = _result()
    second = _event()
    second["experience"] = {"object": "zipper", "result": "works", "pain": "", "itch": ""}
    second["evidence_quote"] = "holds well"
    second["primary_for_review"] = True
    result["events"].append(second)
    result["review_processing"][0]["event_count"] = 2
    assert any("exactly one primary event" in error for error in validate_extract_result(_payload(), result))


def test_long_quote_is_rejected_by_character_limit():
    payload = _payload()
    quote = "The latch holds well during travel " + ("very securely " * 12)
    payload["reviews"][0]["review_text"] = quote
    result = _result()
    result["events"][0]["evidence_quote"] = quote
    assert any("exceeds 160 characters" in error for error in validate_extract_result(payload, result))


def test_normalizer_unwraps_single_json_object_envelope_without_mutating_source():
    original = _result()
    wrapped = [deepcopy(original)]
    normalized = normalize_extract_response(wrapped)
    assert normalized == original
    assert wrapped == [original]


def test_normalizer_combines_per_review_rows_and_derives_only_missing_event_count():
    event = _event()
    rows = [
        {"sample_id": "S-1", "status": "processed_with_events", "reason": "signal", "events": [event]},
        {"sample_id": "S-2", "status": "no_need_signal", "reason": "none", "event_count": 4, "events": []},
    ]
    normalized = normalize_extract_response(rows)
    assert normalized["review_processing"] == [
        {"sample_id": "S-1", "status": "processed_with_events", "reason": "signal", "event_count": 1},
        {"sample_id": "S-2", "status": "no_need_signal", "reason": "none", "event_count": 4},
    ]
    assert normalized["events"][0]["sample_id"] == "S-1"
    assert validate_extract_result(_payload(), normalized)


def test_stable_ids_depend_on_sample_and_event_content_not_chunk_order_and_do_not_mutate_input():
    original = _result()
    before = deepcopy(original)
    first = assign_stable_event_ids(original)
    expanded = deepcopy(original)
    expanded["events"].append(_event("S-2"))
    second = assign_stable_event_ids(expanded)

    assert original == before
    assert first["events"][0]["event_id"] == second["events"][0]["event_id"]
    assert second["events"][0]["event_id"].startswith("EV2-")
    changed_content = deepcopy(original)
    changed_content["events"][0]["experience"]["result"] = "fails under load"
    assert assign_stable_event_ids(changed_content)["events"][0]["event_id"] != first["events"][0]["event_id"]
    reordered = deepcopy(expanded)
    reordered["events"].reverse()
    reordered_ids = {event["sample_id"]: event["event_id"]
                     for event in assign_stable_event_ids(reordered)["events"]}
    expanded_ids = {event["sample_id"]: event["event_id"] for event in second["events"]}
    assert reordered_ids == expanded_ids


def test_duplicate_identical_events_still_get_distinct_deterministic_ids():
    result = _result()
    result["events"].append(deepcopy(result["events"][0]))
    ids = [event["event_id"] for event in assign_stable_event_ids(result)["events"]]
    assert len(ids) == len(set(ids)) == 2


def test_prompt_and_schema_are_category_agnostic_and_schema_parses():
    import json

    prompt = (ROOT / "backend/amazon_voc/runtime/prompts/full_voc_v2_extract.txt").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "backend/amazon_voc/runtime/prompts/full_voc_v2_extract_schema.json").read_text(encoding="utf-8"))
    assert "Amazon 产品评论 VOC" in prompt
    assert "泳衣" not in prompt
    assert "身体维度" in prompt
    assert "event_id" not in schema["properties"]["events"]["items"]["properties"]
    assert set(schema["required"]) == {"review_processing", "events"}
