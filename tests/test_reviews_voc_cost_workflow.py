from __future__ import annotations

import json
import threading
import time

from backend.amazon_reviews import service as reviews
from backend.local_result_cache import LocalResultCache
from backend.usage_ledger import usage_summary


class TranslationResponse:
    model = "shared/fake-text"
    usage = {"prompt_tokens": 150, "completion_tokens": 20,
             "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 50}

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class TranslationClient:
    model = "shared/fake-text"
    base_url = "https://model.invalid/v1"

    def __init__(self):
        self.calls = 0

    def complete(self, messages, **_kwargs):
        self.calls += 1
        rows = json.loads(messages[-1]["content"])
        return TranslationResponse({"translations": [
            {"index": row["index"], "title_zh": "译：" + row["title"],
             "text_zh": "译：" + row["text"]} for row in rows
        ]})


def test_local_cache_is_workspace_scoped_ttl_limited_and_force_refreshable(tmp_path, monkeypatch):
    import backend.local_result_cache as cache_module

    class FakeClock:
        now = 1000
        @classmethod
        def time(cls):
            return cls.now

    monkeypatch.setattr(cache_module, "time", FakeClock)
    cache = LocalResultCache("unit-test", root=tmp_path, ttl_seconds=10)
    key = cache_module.fingerprint({"text": "same review", "schema": "v1"})
    cache.put("workspace-a", key, {"translated": "同一评论"})

    assert cache.get("workspace-a", key) == (True, {"translated": "同一评论"})
    assert cache.get("workspace-b", key) == (False, None)
    assert cache.get("workspace-a", key, force_refresh=True) == (False, None)
    FakeClock.now = 1011
    assert cache.get("workspace-a", key) == (False, None)


def test_translation_cache_hits_across_jobs_and_force_refresh_records_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "ROOT", tmp_path / "reviews")
    monkeypatch.setattr(reviews, "TRANSLATION_CACHE",
                        LocalResultCache("reviews-translation", root=tmp_path / "cache"))
    client = TranslationClient()
    source = {"asin": "B0D5TQJXRN", "title": "Easy fit", "content": "Comfortable for swimming."}

    first = {"id": "a" * 32, "workspace_id": "workspace-translation", "model": client.model}
    reviews._save({**first, "status": "running", "items": []})
    reviews._translate_reviews(client, first, {"asin": source["asin"]}, [dict(source)], threading.Event())
    second = {"id": "b" * 32, "workspace_id": "workspace-translation", "model": client.model}
    reviews._save({**second, "status": "running", "items": []})
    cached_rows = [dict(source)]
    reviews._translate_reviews(client, second, {"asin": source["asin"]}, cached_rows, threading.Event())

    assert client.calls == 1
    assert cached_rows[0]["translation_zh"] == "译：Comfortable for swimming."
    cached_ledger = usage_summary("reviews", second["id"], second["workspace_id"])
    translation = next(stage for stage in cached_ledger["stages"] if stage["stage"] == "reviews_translation")
    assert translation["local_cache_hits"] == 1
    assert translation["requests"] == 0
    assert cached_ledger["cost_status"] == "unknown"

    refreshed = {"id": "c" * 32, "workspace_id": "workspace-translation", "model": client.model,
                 "force_refresh": True}
    reviews._save({**refreshed, "status": "running", "items": []})
    reviews._translate_reviews(client, refreshed, {"asin": source["asin"]}, [dict(source)], threading.Event())
    assert client.calls == 2
    refreshed_stage = next(stage for stage in usage_summary("reviews", refreshed["id"], refreshed["workspace_id"])["stages"]
                           if stage["stage"] == "reviews_translation")
    assert refreshed_stage["input_tokens"] == 150
    assert refreshed_stage["output_tokens"] == 20
    assert refreshed_stage["cache_hit_input_tokens"] == 100
    assert refreshed_stage["cache_miss_input_tokens"] == 50
    assert refreshed_stage["cost_status"] == "unknown"


def test_reviews_modes_default_to_no_translation_model_and_combined_autohandoff(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "ROOT", tmp_path / "reviews")
    monkeypatch.setattr(reviews, "_dispatch_locked", lambda: None)
    monkeypatch.setattr(reviews.sorftime_settings, "connection", lambda: ("https://mcp.invalid", "hidden"))

    combined = reviews.create({
        "mode": "asins", "site": "US", "asins": ["B0D5TQJXRN"],
        "workflow_mode": "combined", "translate_enabled": False,
    })
    reviews_only = reviews.create({
        "mode": "asins", "site": "US", "asins": ["B0GY6VX976"],
        "workflow_mode": "reviews_only", "translate_enabled": False,
    })
    assert combined["auto_voc_policy"] == "any_exported"
    assert combined["translate_enabled"] is False
    assert combined["model"] == ""
    assert reviews_only["auto_voc_policy"] == "none"


def test_reviews_collection_cache_and_force_refresh_keep_exported_workbook(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "ROOT", tmp_path / "reviews")
    monkeypatch.setattr(reviews, "COLLECTION_CACHE",
                        LocalResultCache("reviews-collection", root=tmp_path / "cache"))
    monkeypatch.setattr(reviews, "SHUTTING_DOWN", False)
    monkeypatch.setattr(reviews.sorftime_settings, "connection", lambda: ("https://mcp.invalid", "hidden"))
    calls = []
    response = {"data": [{"review_date": "2026-09-01", "star_rating": 5,
                          "title": "Great fit", "content": "Works for swimming."}]}
    monkeypatch.setattr(reviews.sorftime, "call", lambda *_args: calls.append(1) or response)

    def run_job(job_id, *, force_refresh=False):
        folder = reviews._job_dir(job_id)
        (folder / "raw").mkdir(parents=True, exist_ok=True)
        job = {"id": job_id, "status": "queued", "mode": "asins", "site": "US", "node_id": "",
               "asins": ["B0D5TQJXRN"], "rank_limit": 50, "model": "", "credential_id": "",
               "workspace_id": "workspace-collection", "workflow_mode": "reviews_only",
               "translate_enabled": False, "force_refresh": force_refresh, "auto_voc_policy": "none",
               "created_at": time.time(), "attempt": 0, "items": [{"asin": "B0D5TQJXRN",
               "status": "pending", "review_count": 0, "error": ""}], "errors": [], "review_count": 0,
               "review_collection_contract": reviews.REVIEW_CALL_CONTRACT}
        reviews._save(job)
        reviews._run(job_id, threading.Event())
        return reviews.get(job_id)

    first = run_job("d" * 32)
    cached = run_job("e" * 32)
    refreshed = run_job("f" * 32, force_refresh=True)
    assert len(calls) == 4
    assert first["metrics"]["review_tool_calls"] == 2
    assert cached["metrics"]["review_tool_calls"] == 0
    assert cached["metrics"]["cached_review_requests"] == 2
    assert refreshed["metrics"]["review_tool_calls"] == 2
    assert refreshed["metrics"]["cached_review_requests"] == 0
    assert reviews.output_path(cached["id"]).is_file()
    ledger = usage_summary("reviews", cached["id"], "workspace-collection")
    collection = next(stage for stage in ledger["stages"] if stage["stage"] == "reviews_collection")
    assert collection["local_cache_hits"] == 2
    assert collection["review_api_calls"] == 0


def test_failed_automatic_voc_handoff_retries_from_saved_workbook(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "ROOT", tmp_path / "reviews")
    job_id = "9" * 32
    folder = reviews._job_dir(job_id)
    folder.mkdir(parents=True)
    workbook = folder / "result.xlsx"
    workbook.write_bytes(b"already-exported-review-workbook")
    job = {"id": job_id, "status": "completed", "site": "US", "review_count": 1,
           "auto_voc_policy": "any_exported", "items": [], "voc_handoff": None}
    reviews._save(job)

    class FlakyVOCService:
        calls = 0
        received_hashes = []

        def create_run_from_amazon_reviews(self, source_job_id, confirmed_reviewed):
            self.calls += 1
            assert source_job_id == job_id
            assert confirmed_reviewed is True
            import hashlib
            self.received_hashes.append(hashlib.sha256(workbook.read_bytes()).hexdigest())
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return {"project": {"id": "voc-project"},
                    "run": {"id": "voc-run", "created_at": "2026-10-04T00:00:00Z"}}

    from backend.amazon_voc import service as voc_service_module
    flaky = FlakyVOCService()
    monkeypatch.setattr(voc_service_module, "service", flaky)

    reviews._auto_voc_handoff(job)
    failed = reviews.get(job_id)
    assert failed["voc_handoff"]["status"] == "failed"
    assert workbook.is_file()
    reviews._auto_voc_handoff(reviews._read(job_id))
    retried = reviews.get(job_id)
    assert retried["voc_handoff"]["status"] == "started"
    assert retried["voc_handoff"]["run_id"] == "voc-run"
    assert "error" not in retried["voc_handoff"]
    assert flaky.calls == 2
    assert len(set(flaky.received_hashes)) == 1
