import json
import time
import threading
from types import SimpleNamespace
import pytest

from openpyxl import load_workbook

from backend.amazon_reviews import brightdata, service, sorftime, woot
from backend.keyword_research.llm_client import LLMError
from backend.local_result_cache import LocalResultCache


ASIN_A = "B000000004"
ASIN_B = "B000000005"
NODE = "123456789"


@pytest.fixture(autouse=True)
def isolate_review_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "COLLECTION_CACHE", LocalResultCache("reviews-collection", tmp_path / "cache"))
    monkeypatch.setattr(service, "TRANSLATION_CACHE", LocalResultCache("reviews-translation", tmp_path / "cache"))


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeTranslator:
    def __init__(self, *_args):
        pass

    def complete(self, messages, **_kwargs):
        inputs = json.loads(messages[-1]["content"])
        return FakeResponse({"translations": [
            {"index": row["index"], "title_zh": "译：" + row["title"],
             "text_zh": "译：" + row["text"]}
            for row in inputs
        ]})


class OneBasedTranslator:
    def complete(self, messages, **_kwargs):
        inputs = json.loads(messages[-1]["content"])
        return FakeResponse({"translations": [
            {"index": row["index"] + 1, "title_zh": "译：" + row["title"],
             "text_zh": "译：" + row["text"]}
            for row in inputs
        ]})


def test_sorftime_decode_prefers_structured_content_to_display_text():
    payload = {"data": [{"title": "Useful", "content": "Works well"}]}
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="Fetched 1 review")],
        structuredContent=payload,
    )

    assert sorftime._decode(result, "test-key") == payload


def test_sorftime_decode_treats_no_reviews_message_as_empty_review_payload():
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="No reviews found.")],
        structuredContent=None,
    )

    payload = sorftime._decode(result, "test-key", tool_name="product_reviews")

    assert payload == {"data": [], "message": "No reviews found."}
    assert service._extract_reviews(payload) == []


def test_sorftime_no_reviews_message_is_not_accepted_for_other_tools():
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="No reviews found.")],
        structuredContent=None,
    )

    with pytest.raises(ValueError, match="non-JSON"):
        sorftime._decode(result, "test-key", tool_name="category_report")


def test_sorftime_call_records_no_reviews_as_a_success(monkeypatch):
    finished = []

    async def fake_call(_endpoint, _key, _name, _arguments):
        return {"data": [], "message": "No reviews found."}

    monkeypatch.setattr(sorftime, "_call", fake_call)
    monkeypatch.setattr(sorftime.usage_stats, "start_usage_event", lambda **_kwargs: "event-id")
    monkeypatch.setattr(
        sorftime.usage_stats, "finish_usage_event",
        lambda _event_id, **kwargs: finished.append(kwargs),
    )

    payload = sorftime.call(
        "https://mcp.invalid/mcp", "test-key", "product_reviews", {"asin": ASIN_A},
    )

    assert payload == {"data": [], "message": "No reviews found."}
    assert len(finished) == 1 and finished[0]["status"] == "success"


def test_sorftime_decode_handles_multiple_independent_json_text_blocks():
    result = SimpleNamespace(
        isError=False,
        content=[
            SimpleNamespace(text=json.dumps({"data": [{"title": "First", "content": "A"}]})),
            SimpleNamespace(text=json.dumps({"data": [{"title": "Second", "content": "B"}]})),
        ],
        structuredContent=None,
    )

    payload = sorftime._decode(result, "test-key")
    reviews = service._extract_reviews(payload)

    assert [row["title"] for row in reviews] == ["First", "Second"]


def test_sorftime_decode_classifies_non_json_without_exposing_response_body():
    secret_body = "<html>private upstream response</html>"
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text=secret_body)],
        structuredContent=None,
    )

    with pytest.raises(ValueError, match="HTML") as error:
        sorftime._decode(result, "test-key")

    assert "private upstream response" not in str(error.value)


def test_sorftime_call_retries_a_connect_error_once_and_records_it(monkeypatch):
    class ConnectError(RuntimeError):
        pass

    calls = []
    sleeps = []
    finished = []

    async def fake_call(_endpoint, _key, _name, _arguments):
        calls.append(1)
        if len(calls) == 1:
            raise ExceptionGroup("transport", [ConnectError("connection refused")])
        return {"data": []}

    monkeypatch.setattr(sorftime, "_call", fake_call)
    monkeypatch.setattr(sorftime.time, "sleep", sleeps.append)
    monkeypatch.setattr(sorftime.usage_stats, "start_usage_event", lambda **_kwargs: "event-id")
    monkeypatch.setattr(
        sorftime.usage_stats, "finish_usage_event",
        lambda _event_id, **kwargs: finished.append(kwargs),
    )

    result = sorftime.call(
        "https://mcp.invalid/mcp", "test-key", "product_reviews", {"asin": ASIN_A},
        usage_context={"module": "amazon_reviews"},
    )

    assert result == {"data": []}
    assert len(calls) == 2
    assert sleeps == [0.5]
    assert len(finished) == 1
    assert finished[0]["status"] == "success"
    assert finished[0]["duration_ms"] >= 0
    assert finished[0]["metadata"] == {"transport_retries": 1}


def test_sorftime_call_does_not_retry_non_json_response_errors(monkeypatch):
    calls = []

    async def fake_call(_endpoint, _key, _name, _arguments):
        calls.append(1)
        raise ValueError("Sorftime returned non-JSON review data")

    monkeypatch.setattr(sorftime, "_call", fake_call)
    monkeypatch.setattr(sorftime.usage_stats, "start_usage_event", lambda **_kwargs: "event-id")
    monkeypatch.setattr(sorftime.usage_stats, "finish_usage_event", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="non-JSON"):
        sorftime.call(
            "https://mcp.invalid/mcp", "test-key", "product_reviews", {"asin": ASIN_A},
            usage_context={"module": "amazon_reviews"},
        )

    assert len(calls) == 1


class GenericFieldTranslator:
    def complete(self, messages, **_kwargs):
        inputs = json.loads(messages[-1]["content"])
        return FakeResponse({"translations": [
            {"index": row["index"], "title": "译：" + row["title"],
             "text": "译：" + row["text"]}
            for row in inputs
        ]})


class InvalidThenValidTranslator:
    def __init__(self, *_args):
        self.calls = 0

    def complete(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            class InvalidResponse:
                @staticmethod
                def json():
                    raise LLMError("LLM 输出不是合法 JSON")
            return InvalidResponse()
        assert "上一次回答不是合法 JSON" in messages[-1]["content"]
        inputs = json.loads(messages[-2]["content"])
        return FakeResponse({"translations": [
            {"index": row["index"], "title_zh": "译：" + row["title"],
             "text_zh": "译：" + row["text"]}
            for row in inputs
        ]})


def test_extract_sorftime_category_and_review_envelopes():
    category = {"data": {"top100_products": [{"asin": ASIN_A, "title": "A"}]}}
    reviews = {"content": [{"type": "text", "text": json.dumps({
        "data": [{"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works"}]
    })}]}

    assert service._extract_products(category) == [{
        "asin": ASIN_A, "title": "A", "category": "", "rank": None,
    }]
    assert service._extract_reviews(reviews) == [{
        "review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works",
    }]


def test_review_identity_uses_source_composite_key():
    first = {"asin": ASIN_A, "review_date": "20260901", "title": "Good", "content": "Works", "review_id": "one"}
    second = {**first, "review_id": "different"}
    distinct = {**first, "review_date": "20260902"}

    assert service._review_key(first) == service._review_key(second)
    assert service._review_key(first) != service._review_key(distinct)


def test_reviews_from_calls_preserves_all_variant_attributes_after_deduplication():
    duplicate_rows = [
        {"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works",
         "variant_attribute": "color=black; size=small"},
        {"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works",
         "variant_attribute": "color=navy; size=small"},
        {"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works",
         "variant_attribute": "color=gray; size=small"},
    ]

    reviews, metrics = service._reviews_from_calls([
        {"round": 1, "review_type": "Both", "payload": {"data": duplicate_rows}},
        {"round": 2, "review_type": "Both", "payload": {"data": duplicate_rows}},
    ], ASIN_A)

    assert len(reviews) == 1
    assert reviews[0]["source_variant_attributes"] == [
        "color=black; size=small", "color=navy; size=small", "color=gray; size=small",
    ]
    assert reviews[0]["variant_attribute"] == (
        "color=black; size=small | color=navy; size=small | color=gray; size=small"
    )
    assert [metric["raw_record_count"] for metric in metrics] == [3, 3]
    assert [metric["new_unique_count"] for metric in metrics] == [1, 0]


def test_translation_accepts_one_based_model_indexes(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    job_id = "b" * 32
    job_dir = service._job_dir(job_id)
    (job_dir / "raw").mkdir(parents=True)
    job = {"id": job_id, "status": "running", "stage": "translating"}
    service._save(job)
    item = {"asin": ASIN_A}
    reviews = [
        {"asin": ASIN_A, "title": "Easy to use", "content": "It works well."},
        {"asin": ASIN_A, "title": "Good value", "content": "Worth the price."},
    ]

    service._translate_reviews(OneBasedTranslator(), job, item, reviews, threading.Event())

    assert [row["t_title"] for row in reviews] == ["译：Easy to use", "译：Good value"]
    assert [row["t_content"] for row in reviews] == ["译：It works well.", "译：Worth the price."]


def test_translation_accepts_generic_title_and_text_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    job_id = "c" * 32
    job_dir = service._job_dir(job_id)
    (job_dir / "raw").mkdir(parents=True)
    job = {"id": job_id, "status": "running", "stage": "translating"}
    service._save(job)
    item = {"asin": ASIN_A}
    reviews = [{"asin": ASIN_A, "title": "Worth buying", "content": "It works as promised."}]

    service._translate_reviews(GenericFieldTranslator(), job, item, reviews, threading.Event())

    assert reviews[0]["t_title"] == "译：Worth buying"
    assert reviews[0]["t_content"] == "译：It works as promised."


def test_translation_retries_one_malformed_json_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    job_id = "d" * 32
    job_dir = service._job_dir(job_id)
    (job_dir / "raw").mkdir(parents=True)
    job = {"id": job_id, "status": "running", "stage": "translating"}
    service._save(job)
    translator = InvalidThenValidTranslator()
    reviews = [{"asin": ASIN_A, "title": "Good", "content": "Works well."}]

    service._translate_reviews(translator, job, {"asin": ASIN_A}, reviews, threading.Event())

    assert translator.calls == 2
    assert reviews[0]["t_title"] == "译：Good"
    assert reviews[0]["t_content"] == "译：Works well."


def test_sorftime_exception_groups_keep_leaf_cause_without_secrets():
    error = ExceptionGroup("transport failed", [
        RuntimeError("GET https://mcp.example.test/mcp?key=private-value failed"),
        ValueError("Bearer private-value"),
    ])

    message = sorftime._scrub(sorftime._diagnostic(error), "private-value")

    assert "private-value" not in message
    assert "?key=[hidden]" in message
    assert "RuntimeError" in message and "ValueError" in message


def test_live_shaped_node_job_deduplicates_translates_and_exports(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "model_target", lambda *_args: ("https://model.invalid/v1", "model-secret", "mock/model"))
    monkeypatch.setattr(service, "require_validated", lambda *_args: True)
    monkeypatch.setattr(service, "LLMClient", FakeTranslator)

    calls_by_asin = {}
    api_calls = []

    def fake_sorftime(_endpoint, _key, tool, arguments):
        api_calls.append((tool, arguments))
        if tool == "category_report":
            return {"data": {"top100_products": [
                {"asin": ASIN_A, "title": "Product A", "rank": 1},
                {"asin": ASIN_B, "title": "Product B", "rank": 2},
                {"asin": "B000000000", "title": "Not selected", "rank": 3},
            ]}}
        assert tool == "product_reviews"
        assert arguments["amz_site"] == "US"
        assert arguments["review_type"] == "Both"
        asin = arguments["asin"]
        calls_by_asin[asin] = calls_by_asin.get(asin, 0) + 1
        shared = {
            "variant_attribute": "Blue",
            "review_date": "20260901",
            "star_rating": 5,
            "title": "=Works well",
            "content": "The light is bright and easy to use.",
        }
        new_review = {
            "variant_attribute": "Blue",
            "review_date": "20260902",
            "star_rating": 1,
            "title": "Stopped working",
            "content": "It stopped after one day.",
        }
        return {"data": [shared] if calls_by_asin[asin] == 1 else [shared, new_review]}

    monkeypatch.setattr(service.sorftime, "call", fake_sorftime)
    created = service.create({
        "mode": "node_asins", "site": "US", "node_id": NODE, "asins": [ASIN_A, ASIN_B],
        "rank_limit": 50, "collection_source": "sorftime",
        "model": "mock/model", "credential_id": "mock-key",
    })

    deadline = time.monotonic() + 10
    result = service.get(created["id"])
    while result["status"] not in ("completed", "completed_with_errors", "failed"):
        if time.monotonic() >= deadline:
            raise AssertionError("评论任务未在测试时限内结束")
        time.sleep(0.02)
        result = service.get(created["id"])
    worker = service.ACTIVE.get(created["id"])
    if worker:
        worker.join(timeout=2)
    result = service.get(created["id"])

    assert result["status"] == "completed", result
    assert result["started_at"] >= result["created_at"]
    assert result["finished_at"] >= result["started_at"]
    assert result["duration_seconds"] >= 0
    assert result["total_duration_seconds"] >= result["duration_seconds"]
    assert result["attempt_history"][-1]["status"] == "completed"
    assert result["attempt_history"][-1]["finished_at"] == result["finished_at"]
    assert [item["asin"] for item in result["items"]] == [ASIN_A, ASIN_B]
    assert result["metrics"] == {"review_tool_calls": 4, "cached_review_requests": 0,
                                 "raw_records": 6, "new_unique_records": 4}
    assert len(api_calls) == 5  # one category report and two identical Both calls per ASIN
    assert all(calls_by_asin[asin] == 2 for asin in (ASIN_A, ASIN_B))

    raw = json.loads(service.raw_path(created["id"]).read_text(encoding="utf-8"))
    assert len(raw["reviews"]) == 4
    assert all(row["source_site"] == "US" and row["source_node"] == NODE for row in raw["reviews"])
    assert all(row["t_content"] and row["t_content"] == row["translation_zh"] for row in raw["reviews"])
    assert all(row["t_title"] and row["t_title"] == row["title_zh"] for row in raw["reviews"])
    assert all(row["sentiment"] in ("Positive", "Negative") for row in raw["reviews"])
    assert all(len(raw["review_call_responses"][asin]) == 2 for asin in (ASIN_A, ASIN_B))

    workbook = load_workbook(service.output_path(created["id"]), data_only=True)
    assert workbook.sheetnames == ["全部评论", "商品评论统计", "统计汇总"]
    all_reviews = workbook["全部评论"]
    assert all_reviews.max_row == 5
    assert all_reviews.max_column == 15
    assert all_reviews.cell(1, 1).value == "#"
    assert all_reviews.cell(2, 10).value == "'=Works well"  # Excel formula injection is neutralized
    assert workbook["商品评论统计"].max_row == 3
    summary = {workbook["统计汇总"].cell(row, 1).value: workbook["统计汇总"].cell(row, 2).value
               for row in range(2, workbook["统计汇总"].max_row + 1)}
    assert summary["去重后评论总数"] == 4
    assert summary["API 调用次数"] == 4
    assert summary["翻译覆盖"] == "4/4 条"


def test_attempt_timing_history_preserves_each_retry(monkeypatch):
    moments = iter([100.0, 104.0, 200.0, 209.0])
    monkeypatch.setattr(service.time, "time", lambda: next(moments))
    job = {"id": "a" * 32, "status": "queued", "attempt": 0, "created_at": 90.0}

    service._start_attempt(job)
    job["status"] = "completed_with_errors"
    service._finish_attempt(job)

    job["status"] = "queued"
    job["started_at"] = None
    job["finished_at"] = None
    job["duration_seconds"] = None
    service._start_attempt(job)
    job["status"] = "completed"
    service._finish_attempt(job)

    snapshot = service._job_snapshot(job)
    assert snapshot["attempt"] == 2
    assert snapshot["started_at"] == 200.0
    assert snapshot["finished_at"] == 209.0
    assert snapshot["duration_seconds"] == 9.0
    assert snapshot["total_duration_seconds"] == 13.0
    assert [(item["status"], item["duration_seconds"]) for item in snapshot["attempt_history"]] == [
        ("completed_with_errors", 4.0), ("completed", 9.0),
    ]


def test_recover_records_interruption_time_for_running_attempt(monkeypatch, tmp_path):
    root = tmp_path / "amazon-reviews"
    root.mkdir()
    monkeypatch.setattr(service, "ROOT", root)
    monkeypatch.setattr(service.time, "time", lambda: 200.0)
    job_id = "b" * 32
    folder = root / job_id
    folder.mkdir()
    service._atomic_json(folder / "job.json", {
        "id": job_id, "status": "running", "created_at": 90.0, "attempt": 1,
        "started_at": 100.0, "finished_at": None, "duration_seconds": None,
        "attempt_history": [{"attempt": 1, "status": "running", "started_at": 100.0,
                             "finished_at": None, "duration_seconds": None}],
        "items": [{"asin": ASIN_A, "status": "collecting"}],
    })

    service.recover()

    recovered = service._read(job_id)
    assert recovered["status"] == "interrupted"
    assert recovered["finished_at"] == 200.0
    assert recovered["duration_seconds"] == 100.0
    assert recovered["attempt_history"][0]["status"] == "interrupted"


def test_create_rejects_unconfigured_sorftime(monkeypatch):
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: (_ for _ in ()).throw(ValueError("not configured")))
    monkeypatch.setattr(service, "model_target", lambda *_args: ("https://model.invalid/v1", "secret", "mock/model"))
    monkeypatch.setattr(service, "require_validated", lambda *_args: True)

    try:
        service.create({
            "mode": "asins", "site": "US", "asins": [ASIN_A], "collection_source": "sorftime",
            "model": "mock/model", "credential_id": "mock-key",
        })
    except ValueError as exc:
        assert str(exc) == "not configured"
    else:
        raise AssertionError("未配置 Sorftime 时不应创建任务")


def test_brightdata_source_is_explicit_capped_and_keeps_existing_review_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", True)
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    monkeypatch.setattr(service.brightdata_settings, "connection", lambda: "test-provider-key")
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: pytest.fail("不应调用 Sorftime"))
    monkeypatch.setattr(woot, "collect_reviews", lambda *_args, **_kwargs: pytest.fail("不应调用 Woot"))
    calls = []

    def fake_brightdata(asin, api_key, **kwargs):
        calls.append((asin, api_key, kwargs))
        return {"reviews": [{"asin": asin, "review_date": "2026-09-01", "star_rating": "5",
                             "title": "Good product", "content": "Works as described."}],
                "request_count": 3, "snapshot_id": "s_fixture"}

    monkeypatch.setattr(brightdata, "collect_reviews", fake_brightdata)
    created = service.create({
        "mode": "asins", "site": "US", "asins": [ASIN_A],
        "collection_source": "brightdata", "translate_enabled": False,
    })
    assert created["review_collection_contract"]["max_reviews_per_asin"] == brightdata.MAX_REVIEWS_PER_ASIN
    service._run(created["id"], threading.Event())

    result = service.get(created["id"])
    raw = json.loads(service.raw_path(created["id"]).read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["items"][0]["collection_source"] == "brightdata"
    assert result["collection_sources"]["brightdata_asins"] == 1
    assert result["metrics"]["review_tool_calls"] == 3
    assert calls[0][0:2] == (ASIN_A, "test-provider-key")
    assert calls[0][2]["cancel"].is_set() is False
    assert raw["reviews"][0]["collection_source"] == "brightdata"
    assert service._source_label("brightdata") == "Bright Data"


def test_brightdata_source_requires_us_direct_asins_and_configured_key(monkeypatch):
    monkeypatch.setattr(service.brightdata_settings, "connection", lambda: "test-provider-key")
    with pytest.raises(ValueError, match="美国站的指定 ASIN 模式"):
        service.create({"mode": "ranked", "site": "US", "node_id": NODE,
                        "collection_source": "brightdata", "translate_enabled": False})
    with pytest.raises(ValueError, match="美国站"):
        service.create({"mode": "asins", "site": "GB", "asins": [ASIN_A],
                        "collection_source": "brightdata", "translate_enabled": False})
    monkeypatch.setattr(service.brightdata_settings, "connection", lambda: (_ for _ in ()).throw(
        ValueError("Bright Data not configured")))
    with pytest.raises(ValueError, match="Bright Data not configured"):
        service.create({"mode": "asins", "site": "US", "asins": [ASIN_A],
                        "collection_source": "brightdata", "translate_enabled": False})


def test_us_asin_jobs_default_to_auto_and_validate_sorftime_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", True)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))

    created = service.create({
        "mode": "asins", "site": "US", "asins": [ASIN_A], "translate_enabled": False,
    })

    assert created["collection_source"] == "auto"
    assert created["review_collection_contract"]["source"] == "automatic"

    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: (_ for _ in ()).throw(
        ValueError("Sorftime not configured")))
    with pytest.raises(ValueError, match="Sorftime not configured"):
        service.create({"mode": "asins", "site": "US", "asins": [ASIN_A], "translate_enabled": False})


def test_us_auto_source_does_not_fallback_on_unclassified_woot_error(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    monkeypatch.setattr(woot, "collect_reviews", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("Woot review request failed")))
    monkeypatch.setattr(service.sorftime, "call", lambda *_args: pytest.fail("未分类 Woot 错误不应自动改用 Sorftime"))
    job = _auto_review_job("e" * 32)
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    assert saved["status"] == "completed_with_errors"
    assert saved["items"][0]["status"] == "failed"
    assert "review request failed" in saved["items"][0]["error"]


def test_auto_source_does_not_bypass_external_effects_offline_guard(monkeypatch, tmp_path):
    from backend.external_effects import ExternalEffectsDisabled

    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    monkeypatch.setattr(woot, "collect_reviews", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ExternalEffectsDisabled("offline")))
    monkeypatch.setattr(service.sorftime, "call", lambda *_args: pytest.fail("离线拒绝后不应调用 Sorftime"))
    job = _auto_review_job("1" * 32)
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    assert saved["status"] == "completed_with_errors"
    assert saved["items"][0]["status"] == "failed"
    assert "offline" in saved["items"][0]["error"]


def test_auto_fallback_retry_resumes_sorftime_without_rechecking_woot(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    monkeypatch.setattr(woot, "collect_reviews", lambda *_args, **_kwargs: pytest.fail("已确认目录未匹配后不应重查 Woot"))
    sorftime_calls = []
    row = {"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works well"}

    def fake_sorftime(_endpoint, _key, tool, arguments):
        sorftime_calls.append((tool, arguments))
        return {"data": [row]}

    monkeypatch.setattr(service.sorftime, "call", fake_sorftime)
    job = _auto_review_job("f" * 32)
    item = job["items"][0]
    item.update({"collection_source": "sorftime", "fallback_from": "woot",
                 "fallback_reason": "Woot 完整目录没有精确匹配"})
    job.update({"status": "running", "attempt": 1, "stage": "interrupted"})
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)
    first_call = [{"round": 1, "review_type": "Both", "collection_source": "sorftime",
                   "payload": {"data": [row]}, "cache_hit": False}]
    service._atomic_json(service._call_payloads_path(job["id"], ASIN_A), first_call)
    reviews, item["review_call_metrics"] = service._reviews_from_calls(first_call, ASIN_A)
    service._annotate_reviews(reviews, job, item)
    service._write_reviews(job["id"], ASIN_A, reviews)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    assert saved["status"] == "completed"
    assert len(sorftime_calls) == 1
    assert sorftime_calls[0][1]["asin"] == ASIN_A
    assert saved["items"][0]["collection_source"] == "sorftime"
    assert saved["items"][0]["fallback_from"] == "woot"


def test_retry_resumes_the_missing_review_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "model_target", lambda *_args: ("https://model.invalid/v1", "model-secret", "mock/model"))
    monkeypatch.setattr(service, "require_validated", lambda *_args: True)
    monkeypatch.setattr(service, "LLMClient", FakeTranslator)
    requested = []
    row = {"review_date": "20260901", "star_rating": 5, "title": "Good", "content": "Works well"}

    def fake_sorftime(_endpoint, _key, tool, arguments):
        requested.append((tool, arguments))
        return {"data": [row]}

    monkeypatch.setattr(service.sorftime, "call", fake_sorftime)
    job_id = "a" * 32
    job = {
        "id": job_id, "status": "running", "mode": "asins", "site": "US", "node_id": "",
        "asins": [ASIN_A], "rank_limit": 50, "model": "mock/model", "credential_id": "mock-key",
        "created_at": time.time(), "updated_at": time.time(), "attempt": 1, "stage": "interrupted",
        "review_collection_contract": service.REVIEW_CALL_CONTRACT, "review_count": 1, "errors": [],
        "metrics": {"review_tool_calls": 1, "raw_records": 1, "new_unique_records": 1},
        "items": [{
            "asin": ASIN_A, "title": "Product A", "status": "pending", "review_count": 1, "error": "",
            "review_call_metrics": [{"round": 1, "review_type": "Both", "raw_record_count": 1, "new_unique_count": 1}],
        }],
    }
    service._job_dir(job_id).mkdir(parents=True)
    (service._job_dir(job_id) / "raw").mkdir()
    service._save(job)
    first_call = [{"round": 1, "review_type": "Both", "payload": {"data": [row]}}]
    service._atomic_json(service._call_payloads_path(job_id, ASIN_A), first_call)
    reviews, _ = service._reviews_from_calls(first_call, ASIN_A)
    service._annotate_reviews(reviews, job, job["items"][0])
    service._write_reviews(job_id, ASIN_A, reviews)

    service._run(job_id, threading.Event())
    result = service.get(job_id)
    raw = json.loads(service.raw_path(job_id).read_text(encoding="utf-8"))

    assert result["status"] == "completed"
    assert len(requested) == 1  # only missing pass 2 was called
    assert requested[0][0] == "product_reviews"
    assert requested[0][1] == {"amz_site": "US", "asin": ASIN_A, "review_type": "Both"}
    assert result["metrics"] == {"review_tool_calls": 2, "cached_review_requests": 0,
                                 "raw_records": 2, "new_unique_records": 1}
    assert len(raw["reviews"]) == 1
    assert raw["reviews"][0]["source_review_rounds"] == [1, 2]
    assert raw["reviews"][0]["translation_zh"] == "译：Works well"


def _auto_review_job(job_id, asin=ASIN_A):
    return {
        "id": job_id, "status": "queued", "mode": "asins", "site": "US", "node_id": "",
        "asins": [asin], "rank_limit": 50, "model": "", "credential_id": "",
        "created_at": time.time(), "updated_at": time.time(), "attempt": 0, "stage": "queued",
        "workflow_mode": "reviews_only", "translate_enabled": False, "auto_voc_policy": "none",
        "force_refresh": False, "collection_source": "auto", "woot_review_ids": {},
        "review_collection_contract": {"source": "automatic"}, "review_count": 0, "errors": [],
        "metrics": {"review_tool_calls": 0, "cached_review_requests": 0,
                    "raw_records": 0, "new_unique_records": 0},
        "items": [{"asin": asin, "title": "Example Product", "status": "pending",
                    "review_count": 0, "error": ""}],
    }


def test_sorftime_no_reviews_is_a_completed_zero_review_result(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    calls = []

    def fake_sorftime(_endpoint, _key, tool, arguments):
        calls.append((tool, arguments))
        return {"data": [], "message": "No reviews found."}

    monkeypatch.setattr(service.sorftime, "call", fake_sorftime)
    job = _auto_review_job("e" * 32)
    job["collection_source"] = "sorftime"
    job["review_collection_contract"] = service.REVIEW_CALL_CONTRACT
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    raw = json.loads(service.raw_path(job["id"]).read_text(encoding="utf-8"))
    payloads = json.loads(service._call_payloads_path(job["id"], ASIN_A).read_text(encoding="utf-8"))
    workbook = load_workbook(service.output_path(job["id"]), data_only=True)

    assert saved["status"] == "completed", saved
    assert saved["items"][0]["status"] == "completed"
    assert saved["items"][0]["review_count"] == 0
    assert saved["errors"] == []
    assert len(calls) == 2
    assert all(tool == "product_reviews" for tool, _arguments in calls)
    assert saved["metrics"] == {"review_tool_calls": 2, "cached_review_requests": 0,
                                 "raw_records": 0, "new_unique_records": 0}
    assert raw["reviews"] == []
    assert len(payloads) == 2 and all(call["payload"]["data"] == [] for call in payloads)
    assert workbook.sheetnames == ["全部评论", "商品评论统计", "统计汇总"]
    assert workbook["全部评论"].max_row == 1


@pytest.mark.parametrize("exception_name", [
    "WootCatalogMiss", "WootResolverSearchLimit", "WootCatalogIncomplete",
])
def test_us_auto_source_falls_back_after_woot_miss_or_search_limit(exception_name, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)

    class CatalogMiss(RuntimeError):
        pass

    monkeypatch.setattr(woot, exception_name, CatalogMiss, raising=False)
    woot_calls = []

    def fake_woot(*_args, **_kwargs):
        woot_calls.append("attempted")
        raise CatalogMiss("Woot 完整检索公开目录后未找到精确 ASIN")

    monkeypatch.setattr(woot, "collect_reviews", fake_woot)
    sorftime_calls = []

    def fake_sorftime(_endpoint, _key, tool, arguments):
        sorftime_calls.append((tool, arguments))
        return {"data": [{
            "review_date": "20260901", "star_rating": 5,
            "title": "Fits well", "content": "The product works as expected.",
        }]}

    monkeypatch.setattr(service.sorftime, "call", fake_sorftime)
    job = _auto_review_job("c" * 32)
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    raw = json.loads(service.raw_path(job["id"]).read_text(encoding="utf-8"))
    calls = json.loads(service._call_payloads_path(job["id"], ASIN_A).read_text(encoding="utf-8"))
    workbook = load_workbook(service.output_path(job["id"]), data_only=True)
    all_reviews = workbook["全部评论"]
    summary = {workbook["统计汇总"].cell(row, 1).value: workbook["统计汇总"].cell(row, 2).value
               for row in range(2, workbook["统计汇总"].max_row + 1)}

    assert saved["status"] == "completed", saved
    assert woot_calls == ["attempted"]
    assert len(sorftime_calls) == 2
    assert saved["items"][0]["collection_source"] == "sorftime"
    assert saved["items"][0]["fallback_reason"]
    assert all(call["collection_source"] == "sorftime" for call in calls)
    assert all(review["collection_source"] == "sorftime" for review in raw["reviews"])
    assert all_reviews.max_column == 15
    assert all_reviews.cell(1, 15).value == "评论采集源"
    assert all_reviews.cell(2, 15).value == "Sorftime MCP"
    assert workbook["商品评论统计"].cell(1, 8).value == "采集来源"
    assert workbook["商品评论统计"].cell(2, 8).value == "Sorftime MCP"
    assert "Sorftime" in summary["评论采集方式"]
    assert summary["自动回退 ASIN 数"] == 1


def test_us_auto_source_keeps_woot_results_without_calling_sorftime(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("https://mcp.invalid/mcp", "test-secret"))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_register_project_artifacts", lambda _job: None)
    monkeypatch.setattr(service, "_auto_voc_handoff", lambda _job: None)
    woot_calls = []

    def fake_woot(asin, **_kwargs):
        woot_calls.append(asin)
        return {"reviews": [{
            "review_date": "20260901", "star_rating": 5,
            "title": "Good quality", "content": "This works well.",
        }], "request_count": 20, "mode": "max"}

    monkeypatch.setattr(woot, "collect_reviews", fake_woot)
    monkeypatch.setattr(service.sorftime, "call", lambda *_args: pytest.fail("Woot 命中时不应调用 Sorftime"))
    job = _auto_review_job("d" * 32)
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)

    service._run(job["id"], threading.Event())

    saved = service._read(job["id"])
    raw = json.loads(service.raw_path(job["id"]).read_text(encoding="utf-8"))
    calls = json.loads(service._call_payloads_path(job["id"], ASIN_A).read_text(encoding="utf-8"))
    assert saved["status"] == "completed", saved
    assert woot_calls == [ASIN_A]
    assert saved["items"][0]["collection_source"] == "woot"
    assert all(review["collection_source"] == "woot" for review in raw["reviews"])
    assert len(calls) == 1 and calls[0]["collection_source"] == "woot"


def test_woot_cancellation_is_passed_through_and_keeps_cancelled_task_semantics(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ROOT", tmp_path / "amazon-reviews")
    monkeypatch.setattr(service, "SHUTTING_DOWN", False)
    monkeypatch.setattr(service.sorftime_settings, "connection", lambda: ("", ""))
    monkeypatch.setattr(service, "record_usage", lambda *_args, **_kwargs: None)
    observed = []

    class WootCancelled(RuntimeError):
        pass

    monkeypatch.setattr(woot, "WootCancelled", WootCancelled, raising=False)

    def fake_woot(_asin, *, cancel, **_kwargs):
        observed.append(cancel)
        cancel.set()
        raise WootCancelled("Woot 采集已取消")

    monkeypatch.setattr(woot, "collect_reviews", fake_woot)
    job = _auto_review_job("e" * 32)
    service._job_dir(job["id"]).mkdir(parents=True)
    (service._job_dir(job["id"]) / "raw").mkdir()
    service._save(job)
    cancel = threading.Event()

    service._run(job["id"], cancel)

    saved = service._read(job["id"])
    assert observed == [cancel]
    assert saved["status"] == "cancelled"
    assert saved["started_at"] <= saved["finished_at"]
    assert saved["duration_seconds"] >= 0
    assert saved["attempt_history"][-1]["status"] == "cancelled"
    assert saved["items"][0]["status"] == "pending"
    assert saved["items"][0]["error"] == ""
