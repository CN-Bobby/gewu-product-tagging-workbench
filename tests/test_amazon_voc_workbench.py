"""Phase 2 integration contract for the single-workbook Amazon VOC workbench page."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from backend.amazon_voc import router as voc_router
from backend.amazon_voc import service as voc_service_module
from backend.amazon_voc.runtime.voc_app.pipeline import (
    Pipeline, build_journey_stage_proposal_input, build_mapping_candidates,
    build_mapping_candidate_batches, build_segment_proposal_batches,
)
from backend.amazon_voc.runtime.voc_app import pipeline as pipeline_module
from backend.amazon_voc.service import AmazonVOCService
from backend.local_result_cache import LocalResultCache
from backend.usage_ledger import usage_summary


def review_workbook() -> bytes:
    from io import BytesIO
    book = Workbook()
    sheet = book.active
    sheet.title = "Reviews"
    sheet.append(["ASIN", "Title", "Rating", "Review", "Date"])
    for i in range(3):
        sheet.append(["B001TEST", "Fixture swim top", 2,
                      f"The support feels unstable and takes too long to adjust. Review {i}.",
                      "2026-09-01"])
    output = BytesIO()
    book.save(output)
    return output.getvalue()


SYNTHESIS_DIMENSIONS = (
    "users", "buyers", "relationships", "physical_constraints", "skill_levels", "role_needs",
    "scenario", "motivation", "task", "pains", "itches", "desired_outcomes",
    "product_experience", "workarounds",
)


def test_deepseek_shared_model_uses_responses_schema_mode():
    choose = voc_service_module._shared_model_protocol

    assert choose("https://api.deepseek.com", "deepseek-v4-flash") == "responses"
    assert choose("https://api.deepseek.com/v1", "deepseek-v4-pro") == "responses"
    assert choose("https://model.invalid/v1", "deepseek-v4-flash") == "chat"
    assert choose("https://api.openai.com/v1", "gpt-5-mini") == "chat"


def segment_result(segment_id, name, key):
    key = dict(key)
    return {
        "segment_id": segment_id, "name": name, "name_en": f"{name} (English)",
        "segment_key": key,
        "segment_key_en": {field: f"English {value}" if value else ""
                           for field, value in key.items()},
        "persona_summary": "测试画像摘要", "persona_summary_en": "Test persona summary",
        "jtbd_statement": "测试核心任务", "jtbd_statement_en": "Test core job",
        "merge_reason": "测试合并依据", "merge_reason_en": "Test merge rationale",
        "split_reason": "测试拆分依据", "split_reason_en": "Test split rationale",
        "profile_synthesis": {field: {"zh": f"归纳-{field}", "en": f"Synthesis-{field}"}
                              for field in SYNTHESIS_DIMENSIONS},
        "need_priorities": [],
    }


def stage_result(stage_id, name, order=1, definition="测试旅程阶段"):
    return {"stage_id": stage_id, "name": name, "name_en": f"{name} (English)",
            "order": order, "definition": definition, "definition_en": "Test journey stage"}


class DeterministicVOCProvider:
    """Small schema-compatible fake; keeps integration tests independent of paid APIs."""
    def call(self, task, model, instructions, payload, schema):
        from backend.amazon_voc.runtime.voc_app.quality import iter_values

        if task in {"build_dictionary", "assign_segments", "build_journey"}:
            assert all("review_text" not in event for event in payload.get("events", [])), task

        def event_for(review):
            return {
                "review_id": review["review_id"], "evidence_quote": "support feels unstable",
                "audience": {"user": "明确使用者", "buyer": "本人", "relationship": "自购自用",
                             "physical_constraints": [], "skill_level": "普通使用者", "role_needs": []},
                "scenario": {"context": "日常使用", "place": "", "environmental_stressors": [],
                             "social_emotional_context": "", "trigger": "需要完成任务",
                             "journey_action": "执行核心动作"},
                "motivation": {"explicit": "完成任务", "inferred": "", "inference_basis": []},
                "task": {"core_job": "稳定完成任务", "functional_job": "稳定完成任务",
                         "emotional_job": "", "social_job": "", "desired_outcomes": ["结果稳定"]},
                "experience": {"object": "测试产品", "component": "核心部件", "attribute": "稳定性",
                               "result": "结果不稳定", "pain": ["容易失败"], "itch": ["希望更省时"],
                               "sentiment": "负面", "severity": "中"},
                "workaround": {"exists": False, "raw_action": "", "standardized_action": "",
                               "strategy": "", "recovery_goal": "", "costs": [], "effect": ""},
                "fact_inference": "事实", "confidence": "高",
            }

        if task == "extract_events":
            result = {"reviews": [{"review_id": row["review_id"], "status": "已生成事件",
                                   "no_signal_reason": "", "events": [event_for(row)]}
                                  for row in payload["reviews"]]}
        elif task == "review_events":
            result = {"decisions": [{"event_id": row["event_id"], "action": "accept",
                                     "reason": "证据匹配", "replacement": None}
                                    for row in payload["events"]]}
        elif task == "build_dictionary":
            grouped = {}
            for event in payload["events"]:
                for field, raw, namespace in iter_values(event):
                    grouped.setdefault((namespace, raw), []).append(event["event_id"])
            result = {"tags": [
                {"tag_id": f"TAG-{i:03d}", "namespace": namespace, "canonical_name": raw,
                 "canonical_name_en": f"English {raw}",
                 "definition": f"样本中的{namespace}表达", "definition_en": f"Sample {namespace} expression",
                 "parent_tag_id": "",
                 "raw_variants": [raw], "include_examples": [raw], "exclude_examples": [],
                 "support_event_ids": sorted(set(event_ids)), "confidence": "高"}
                for i, ((namespace, raw), event_ids) in enumerate(sorted(grouped.items()), 1)
            ]}
        elif task == "map_dictionary":
            lookup = {(item["namespace"], item["canonical_name"]): item["tag_id"]
                      for item in payload["tag_dictionary"]}
            result = {"mappings": [
                {"candidate_id": candidate["candidate_id"],
                 "tag_id": lookup[(candidate["namespace"], candidate["raw_value"])]}
                for candidate in payload["candidates"]
            ]}
        elif task == "propose_segments":
            result = {"segments": [segment_result("SEG-DAILY", "日常稳定任务者", {
                "audience": "明确使用者", "scenario": "日常使用",
                "motivation": "完成任务", "task": "稳定完成任务"})]}
        elif task == "assign_segments":
            result = {"assignments": [{"event_id": event["event_id"], "segment_id": "SEG-DAILY",
                                       } for event in payload["events"]]}
        elif task == "build_journey":
            stages = [stage_result("J-USE", "执行任务", 1, "执行核心动作并获得结果")]
            assignments = [] if not payload["fixed_stages"] else [
                {"event_id": event["event_id"], "stage_id": "J-USE"}
                for event in payload["events"]]
            result = {"stages": stages, "assignments": assignments}
        else:
            raise AssertionError(f"unexpected deterministic model task: {task}")
        return result, {"latency_ms": 1, "input_tokens": 10, "output_tokens": 10}


@pytest.fixture
def voc(tmp_path, monkeypatch):
    instance = AmazonVOCService(root=tmp_path / "amazon-voc")
    monkeypatch.setattr(voc_router, "service", instance)
    monkeypatch.setattr(instance, "resolve_shared_model", lambda model_ref=None, credential_id=None: {
        "model_ref": model_ref or "shared/fake-text", "credential_id": credential_id or "cred-1",
        "base_url": "https://model.invalid/v1", "api_key": "test-secret-never-persist",
        "model": "fake-text",
    })
    monkeypatch.setattr(instance, "public_model_settings", lambda: {
        "configured": True, "default_model": "shared/fake-text", "default_credential_id": "cred-1",
        "providers": [{"name": "shared", "model_refs": ["shared/fake-text"],
                       "credentials": [{"id": "cred-1", "name": "Shared key",
                                        "enabled": True, "authorized": True}]}],
    })
    return instance


def client():
    from backend.app import app
    return TestClient(app)


def create_project(api):
    response = api.post("/api/amazon-voc/projects", json={
        "name": "Phase 2 fixture", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_single_workbook_upload_uses_shared_text_model_and_isolates_storage(voc):
    api = client()
    settings = api.get("/api/amazon-voc/settings")
    assert settings.status_code == 200
    assert settings.json()["default_model"] == "shared/fake-text"
    assert "test-secret-never-persist" not in settings.text

    project = create_project(api)
    uploaded = api.post(f"/api/amazon-voc/projects/{project['id']}/runs",
        data={"model": "shared/fake-text", "credential_id": "cred-1"},
        files={"reviews": ("reviews.xlsx", review_workbook(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert uploaded.status_code == 200, uploaded.text
    run = uploaded.json()
    assert run["status"] == "uploaded"
    assert run["sources"]["reviews_sha256"]
    assert set(run["sources"]) == {
        "reviews", "reviews_sha256", "analysis_mode", "review_source_context"
    }
    assert run["sources"]["analysis_mode"] == "full_voc_v2"
    assert run["sources"]["review_source_context"] is None
    snapshot = voc.db.one("SELECT * FROM workbench_model_snapshots WHERE run_id=?", (run["id"],))
    assert snapshot == {"run_id": run["id"], "model_ref": "shared/fake-text", "credential_id": "cred-1"}
    assert "test-secret-never-persist" not in voc.db.path.read_text(encoding="utf-8", errors="ignore")
    assert run["storage_scope"] == "amazon-voc"

    duplicate = api.post(f"/api/amazon-voc/projects/{project['id']}/runs",
        files=[("reviews", ("one.xlsx", review_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
               ("extra", ("two.xlsx", review_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))])
    assert duplicate.status_code == 422


def test_deterministic_pipeline_reports_artifacts_and_revision_rerun(voc):
    api = client()
    project = create_project(api)
    response = api.post(f"/api/amazon-voc/projects/{project['id']}/runs",
        files={"reviews": ("reviews.xlsx", review_workbook(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]

    # This test preserves legacy V4 artifact/revision coverage. New API-created
    # product runs use the dedicated V2 integration test above.
    sources = voc.db.get_run(run_id)["sources"]
    sources["analysis_mode"] = "multi_stage_v4"
    voc.db.update_run(run_id, source_json=json.dumps(sources, ensure_ascii=False))

    voc.run_pipeline(run_id, provider=DeterministicVOCProvider())
    run = api.get(f"/api/amazon-voc/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["model_ref"] == "shared/fake-text"
    assert run["model_calls"]
    html = api.get(f"/api/amazon-voc/runs/{run_id}/artifacts/html")
    excel = api.get(f"/api/amazon-voc/runs/{run_id}/artifacts/excel")
    assert html.status_code == excel.status_code == 200
    html_text = html.content.decode("utf-8")
    assert "01 · 数据与方法概览" in html_text
    assert html_text.count('class="module') == 6
    assert 'class="overview-grid"' in html_text
    assert 'class="comparison-grid"' in html_text
    assert 'class="journey-stage-grid"' in html_text
    assert "User synthesis" in html_text
    assert 'lang="en"' in html_text
    assert "Alexa" not in html_text
    from io import BytesIO
    workbook = load_workbook(BytesIO(excel.content), read_only=True)
    assert len(workbook.sheetnames) == 12
    assert "外部补全" not in workbook.sheetnames
    assert "AI产品建议" not in workbook.sheetnames
    assert workbook["正式与探索细分"][1][2].value == "Segment name (English)"
    assert workbook["细分需求"][1][5].value == "Model synthesis (English)"
    assert workbook["动态旅程"][1][3].value == "Stage (English)"
    workbook.close()

    events = api.get(f"/api/amazon-voc/runs/{run_id}/events").json()["events"]
    revised = dict(events[0])
    revised["experience"] = dict(revised["experience"])
    revised["experience"]["pain"] = ["用户人工修订痛点"]
    patched = api.patch(f"/api/amazon-voc/runs/{run_id}/events/{revised['event_id']}",
                        json={"event": revised, "note": "phase-2 test"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["revision"] == 2
    voc.run_pipeline(run_id, provider=DeterministicVOCProvider())
    run = api.get(f"/api/amazon-voc/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["current_revision"] == 2
    assert "test-secret-never-persist" not in json.dumps(run, ensure_ascii=False)


def test_non_excel_input_is_rejected_and_secret_never_returned(voc):
    api = client()
    project = create_project(api)
    response = api.post(f"/api/amazon-voc/projects/{project['id']}/runs",
        files={"reviews": ("reviews.csv", b"review text", "text/csv")})
    assert response.status_code == 422
    assert "test-secret-never-persist" not in response.text


def test_cancel_retry_and_startup_recovery_keep_the_model_snapshot(voc):
    api = client()
    project = create_project(api)
    response = api.post(f"/api/amazon-voc/projects/{project['id']}/runs",
        files={"reviews": ("reviews.xlsx", review_workbook(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]
    sources = voc.db.get_run(run_id)["sources"]
    sources["analysis_mode"] = "multi_stage_v4"
    voc.db.update_run(run_id, source_json=json.dumps(sources, ensure_ascii=False))
    cancelled = api.post(f"/api/amazon-voc/runs/{run_id}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    retried = api.post(f"/api/amazon-voc/runs/{run_id}/retry", json={})
    assert retried.status_code == 200
    assert retried.json()["status"] == "uploaded"
    voc.db.update_run(run_id, status="extracting", worker_id="stale-worker")
    voc.recover()
    assert voc.db.get_run(run_id)["worker_id"] == ""
    assert voc.model_snapshot(run_id)["model_ref"] == "shared/fake-text"
    completed = voc.run_pipeline(run_id, provider=DeterministicVOCProvider())
    assert completed["status"] == "completed"


def test_pipeline_tuning_env_reaches_workbench_without_overriding_selected_model(voc, monkeypatch):
    tuning = {
        "VOC_BATCH_REVIEWS": "16",
        "VOC_BATCH_CHARS": "20000",
        "VOC_CONCURRENCY": "3",
        "VOC_MAX_REPAIRS": "0",
    }
    for name, value in tuning.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("VOC_MODEL_BASE_URL", "https://untrusted.invalid/v1")
    monkeypatch.setenv("VOC_MODEL_API_KEY", "env-secret-must-not-be-used")
    monkeypatch.setenv("VOC_MODEL_PROTOCOL", "responses")
    monkeypatch.setenv("VOC_EXTRACTOR_MODEL", "env-model-must-not-be-used")
    monkeypatch.setenv("VOC_REVIEWER_MODEL", "env-reviewer-must-not-be-used")

    project = voc.create_project({
        "name": "Runtime tuning fixture", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    captured = {}

    class PipelineSpy:
        def __init__(self, settings, db, storage, provider):
            captured["settings"] = settings

        def process(self, run_id):
            captured["run_id"] = run_id

    monkeypatch.setattr(voc_service_module, "Pipeline", PipelineSpy)
    result = voc.run_pipeline(run["id"], provider=object())

    settings = captured["settings"]
    assert captured["run_id"] == run["id"]
    assert result["id"] == run["id"]
    assert (settings.batch_reviews, settings.batch_chars, settings.concurrency, settings.max_repairs) == (
        16, 20_000, 3, 0,
    )
    assert settings.data_dir == voc.root
    assert settings.database_path == voc.db.path
    assert settings.model_base_url == "https://model.invalid/v1"
    assert settings.model_api_key == "test-secret-never-persist"
    assert settings.model_protocol == "chat"
    assert settings.extractor_model == settings.reviewer_model == "fake-text"
    assert "env-secret-must-not-be-used" not in str(settings)


def test_persistently_unverifiable_review_events_are_excluded_after_two_rounds(voc):
    project = voc.create_project({
        "name": "Evidence exclusion fixture", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    source = "The real review says the fit feels secure during normal activity."
    invalid = {
        "event_id": "event-invalid-quote", "review_id": "review-1", "asin": "B001TEST",
        "rating": 5, "review_date": "2026-09-01", "review_text": source,
        "evidence_quote": "The product is surprisingly comfortable for every possible occasion.",
        "confidence": "低", "motivation": {"inferred": "", "inference_basis": []},
    }
    valid = {
        "event_id": "event-valid-quote", "review_id": "review-1", "asin": "B001TEST",
        "rating": 5, "review_date": "2026-09-01", "review_text": source,
        "evidence_quote": "fit feels secure", "confidence": "高",
        "motivation": {"inferred": "", "inference_basis": []},
    }
    (cache / "events.json").write_text(json.dumps({
        "schema": "amazon-voc-events/v4", "metadata": {}, "events": [invalid, valid],
    }, ensure_ascii=False), encoding="utf-8")
    (cache / "review_processing.json").write_text(json.dumps({
        "reviews": [{"review_id": "review-1", "status": "已生成事件", "event_count": 2, "reason": ""}],
    }, ensure_ascii=False), encoding="utf-8")
    (cache / "semantic_review.json").write_text(json.dumps({
        "selected_event_ids": [invalid["event_id"], valid["event_id"]],
        "selection_rule": "低置信/无依据推断/无效原文证据 + ASIN×评分10%分层抽样",
        "decisions": [
            {"event_id": invalid["event_id"], "action": "accept", "reason": "复核fixture", "replacement": None},
            {"event_id": valid["event_id"], "action": "accept", "reason": "复核fixture", "replacement": None},
        ],
        "unresolved_quote_event_ids": [invalid["event_id"]], "missing_event_ids": [], "complete": False,
    }, ensure_ascii=False), encoding="utf-8")
    (cache / "coverage_rounds.json").write_text(json.dumps({"review": 2}), encoding="utf-8")

    class NoCallProvider:
        def call(self, *args, **kwargs):
            raise AssertionError("already retried evidence must be excluded without another model call")

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
    )
    pipeline = Pipeline(settings, voc.db, voc.storage, NoCallProvider())
    pipeline._review(voc.db.get_run(run["id"]), 1, workdir)
    events = json.loads((cache / "events.json").read_text(encoding="utf-8"))["events"]
    review = json.loads((cache / "semantic_review.json").read_text(encoding="utf-8"))
    processing = json.loads((cache / "review_processing.json").read_text(encoding="utf-8"))["reviews"]
    assert [event["event_id"] for event in events] == [valid["event_id"]]
    assert review["complete"] is True
    assert review["unresolved_quote_event_ids"] == []
    assert review["dropped_unverifiable_event_ids"] == [invalid["event_id"]]
    assert review["dropped_unverifiable_event_count"] == 1
    assert processing[0]["event_count"] == 1


def test_extraction_resume_recovers_cached_events_and_repairs_only_missing_reviews(voc):
    project = voc.create_project({
        "name": "Extraction coverage recovery", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    source = "The support feels unstable and takes too long to adjust."
    cleaned = [{"review_id": "review-1", "asin": "B001TEST", "content": source, "rating": 2,
                "review_date": "2026-09-01"},
               {"review_id": "review-2", "asin": "B001TEST", "content": source, "rating": 2,
                "review_date": "2026-09-02"},
               {"review_id": "review-3", "asin": "B001TEST", "content": source, "rating": 2,
                "review_date": "2026-09-03"}]
    (cache / "cleaned_reviews.json").write_text(json.dumps({"rows": cleaned}), encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"marketplace": "US", "category": "Swimwear"}), encoding="utf-8")
    (cache / "events.json").write_text(json.dumps({"schema": "amazon-voc-events/v4", "metadata": {},
        "events": [{"event_id": "review-1-事01", "review_id": "review-1"}]}), encoding="utf-8")
    duplicate_no_signal = {"review_id": "review-2", "status": "无可用需求信号",
                           "reason": "明确无需求信号", "event_count": 0}
    (cache / "review_processing.json").write_text(json.dumps({"reviews": [
        duplicate_no_signal, dict(duplicate_no_signal)]}), encoding="utf-8")

    class ExplicitNoSignalProvider:
        calls = []
        def call(self, task, model, instructions, payload, schema):
            self.calls.append([row["review_id"] for row in payload["reviews"]])
            return {"reviews": [{"review_id": row["review_id"], "status": "无可用需求信号",
                                  "no_signal_reason": "评论未出现可识别的产品需求线索", "events": []}
                                 for row in payload["reviews"]]}, {"latency_ms": 1}

    provider = ExplicitNoSignalProvider()
    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
    )
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._reconcile_extraction_coverage(voc.db.get_run(run["id"]), 1, workdir)

    processing = json.loads((cache / "review_processing.json").read_text(encoding="utf-8"))["reviews"]
    assert {row["review_id"]: row["status"] for row in processing} == {
        "review-1": "已生成事件", "review-2": "无可用需求信号", "review-3": "无可用需求信号",
    }
    assert provider.calls == [["review-3"]]
    assert len(processing) == 3
    assert next(row for row in processing if row["review_id"] == "review-1")["event_count"] == 1


def test_raw_unmerged_tags_preserve_dictionary_coverage_without_claiming_normalization(voc):
    project = voc.create_project({
        "name": "Raw vocabulary fallback", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    event = {"event_id": "event-1", "review_id": "review-1", "asin": "B001TEST", "rating": 5,
             "review_date": "2026-09-01", "review_text": "The squeaker is too loud but the toy is fun.",
             "evidence_quote": "squeaker is too loud", "confidence": "高",
             "audience": {"user": "dog owner"}, "scenario": {}, "motivation": {}, "task": {},
             "experience": {"object": "squeaky toy", "pain": ["too loud"]}, "workaround": {}}
    (cache / "events.json").write_text(json.dumps({"schema": "amazon-voc-events/v4",
        "metadata": {"model": "fake-text", "prompt_version": "v1", "generated_at": "2026-10-03"},
        "events": [event]}), encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"marketplace": "US", "category": "Pet Toys"}), encoding="utf-8")

    class PainOnlyDictionaryProvider:
        calls = []
        def call(self, task, model, instructions, payload, schema):
            self.calls.append(task)
            from backend.amazon_voc.runtime.voc_app.quality import iter_values
            if task == "build_dictionary":
                values = {}
                for item in payload["events"]:
                    for path, raw, namespace in iter_values(item):
                        if namespace == "pain":
                            values.setdefault(raw, []).append(item["event_id"])
                return {"tags": [{"tag_id": "TAG-PAIN", "namespace": "pain", "canonical_name": raw,
                    "canonical_name_en": "Normalized pain", "definition": "模型归一痛点",
                    "definition_en": "Model-normalized pain", "parent_tag_id": "", "raw_variants": [raw],
                    "include_examples": [raw], "exclude_examples": [],
                    "support_event_ids": sorted(set(event_ids)), "confidence": "高"}
                    for raw, event_ids in values.items()]}, {"latency_ms": 1}
            if task == "map_dictionary":
                lookup = {(tag["namespace"], tag["canonical_name"]): tag["tag_id"]
                          for tag in payload["tag_dictionary"]}
                return {"mappings": [{"candidate_id": candidate["candidate_id"],
                    "tag_id": lookup[(candidate["namespace"], candidate["raw_value"])]}
                    for candidate in payload["candidates"]
                    if (candidate["namespace"], candidate["raw_value"]) in lookup]}, {"latency_ms": 1}
            raise AssertionError(task)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        max_repairs=0,
    )
    provider = PainOnlyDictionaryProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._normalize(voc.db.get_run(run["id"]), 1, workdir)
    dictionary = json.loads((cache / "tag_dictionary.json").read_text(encoding="utf-8"))
    assert dictionary["metadata"]["mapping_missing_count"] == 0
    assert dictionary["metadata"]["normalized_tag_count"] == 1
    assert dictionary["metadata"]["raw_unmerged_tag_count"] > 0
    assert all(tag["normalization_status"] == "raw_unmerged" and "未执行同义词归并" in tag["definition"]
               for tag in dictionary["tags"] if tag["normalization_status"] == "raw_unmerged")
    assert any(row["mapping_method"] == "raw_passthrough" for row in dictionary["mappings"])
    assert dictionary["metadata"]["mapping_model_unmapped_count"] > 0
    provider_call_count = len(provider.calls)
    pipeline._normalize(voc.db.get_run(run["id"]), 1, workdir)
    assert len(provider.calls) == provider_call_count

    (cache / "cleaned_reviews.json").write_text(json.dumps({"rows": [{"review_id": "review-1",
        "asin": "B001TEST", "content": event["review_text"], "rating": 5, "review_date": "2026-09-01"}]}),
        encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"marketplace": "US", "category": "Pet Toys",
        "source_files": {"reviews": "reviews.xlsx"}}), encoding="utf-8")
    (cache / "review_processing.json").write_text(json.dumps({"reviews": [{"review_id": "review-1",
        "status": "已生成事件", "reason": "", "event_count": 1}]}), encoding="utf-8")
    (cache / "semantic_review.json").write_text(json.dumps({"selected_event_ids": ["event-1"],
        "decisions": [{"event_id": "event-1", "action": "accept"}], "complete": True,
        "missing_event_ids": [], "unresolved_quote_event_ids": []}), encoding="utf-8")
    (cache / "segment_model.json").write_text(json.dumps({"segments": [{"segment_key": {
        "audience": "dog owner", "scenario": "", "motivation": "", "task": ""},
        "member_event_ids": ["event-1"]}], "journey": {"event_assignments": [
        {"event_id": "event-1", "stage_id": "stage-1"}]}}), encoding="utf-8")
    from backend.amazon_voc.runtime.voc_app.quality import evaluate
    report = evaluate(workdir)
    dictionary_gate = next(gate for gate in report["gates"] if gate["gate_id"] == 5)
    assert dictionary_gate["pass"] is True
    assert report["counts"]["normalized_tags"] == 1
    assert report["counts"]["raw_unmerged_tags"] > 0

    missing_translation = json.loads(json.dumps(dictionary))
    normalized = next(tag for tag in missing_translation["tags"] if tag["normalization_status"] == "normalized")
    normalized["canonical_name_en"] = ""
    (cache / "tag_dictionary.json").write_text(json.dumps(missing_translation), encoding="utf-8")
    untranslated_report = evaluate(workdir)
    untranslated_gate = next(gate for gate in untranslated_report["gates"] if gate["gate_id"] == 5)
    assert untranslated_gate["pass"] is False
    assert any(check["name"] == "tag fields complete" and not check["pass"]
               for check in untranslated_gate["checks"])

    raw_only_dictionary = json.loads(json.dumps(dictionary))
    for tag in raw_only_dictionary["tags"]:
        tag["normalization_status"] = "raw_unmerged"
        tag["definition"] = "原始字段值透传，未执行同义词归并；不代表已确认的标准术语。"
    for mapping in raw_only_dictionary["mappings"]:
        mapping["mapping_method"] = "raw_passthrough"
    (cache / "tag_dictionary.json").write_text(json.dumps(raw_only_dictionary), encoding="utf-8")
    raw_only_report = evaluate(workdir)
    raw_only_gate = next(gate for gate in raw_only_report["gates"] if gate["gate_id"] == 5)
    assert raw_only_gate["pass"] is False
    assert any(check["name"] == "at least one normalized tag when values exist" and not check["pass"]
               for check in raw_only_gate["checks"])


def test_unbounded_inference_is_withheld_from_reused_review_cache(voc):
    project = voc.create_project({
        "name": "Inference boundary", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "events.json").write_text(json.dumps({"events": [{"event_id": "event-1",
        "motivation": {"explicit": "明确动机", "inferred": "无法核验的推断", "inference_basis": []}}]}),
        encoding="utf-8")
    (cache / "semantic_review.json").write_text(json.dumps({"complete": True, "decisions": []}), encoding="utf-8")

    withheld_count = Pipeline._withhold_unbounded_inferences(workdir)
    events = json.loads((cache / "events.json").read_text(encoding="utf-8"))["events"]
    review = json.loads((cache / "semantic_review.json").read_text(encoding="utf-8"))
    assert withheld_count == 1
    assert events[0]["motivation"] == {"explicit": "明确动机", "inferred": "", "inference_basis": []}
    assert review["unbounded_inference_withheld_count"] == 1


def test_journey_cache_deduplicates_repeated_model_assignments(voc):
    project = voc.create_project({
        "name": "Journey duplicate protection", "marketplace": "US", "country": "US",
        "language": "en", "category": "Swimwear",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "events.json").write_text(json.dumps({"events": [
        {"event_id": "event-1", "review_id": "review-1"},
    ]}), encoding="utf-8")
    (cache / "segment_model.json").write_text(json.dumps({"segments": [
        {"segment_id": "segment-1", "member_event_ids": ["event-1"]},
    ], "journey": {}}), encoding="utf-8")

    class DuplicateJourneyProvider:
        def call(self, task, model, instructions, payload, schema):
            if not payload["fixed_stages"]:
                self.stage_payload = payload
                return {"stages": [stage_result("stage-1", "使用", 1, "用户实际使用产品")],
                        "assignments": []}, {"latency_ms": 1}
            duplicate = {"event_id": "event-1", "stage_id": "stage-1"}
            return {"stages": [], "assignments": [duplicate, dict(duplicate)]}, {"latency_ms": 1}

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
    )
    provider = DuplicateJourneyProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._journey(voc.db.get_run(run["id"]), 1, workdir)
    model = json.loads((cache / "segment_model.json").read_text(encoding="utf-8"))
    assert provider.stage_payload["event_count"] == 1
    assert "events" not in provider.stage_payload
    assert provider.stage_payload["segments"][0]["member_event_count"] == 1
    assert model["journey"]["event_assignments"] == [
        {"event_id": "event-1", "stage_id": "stage-1"},
    ]
    assert model["journey"]["stage_generation"]["sampled_event_count"] == 1


def test_journey_stage_proposal_is_bounded_and_stratified_without_raw_reviews():
    events = []
    segments = []
    for segment_index in range(3):
        member_ids = []
        for event_index in range(30):
            event_id = f"event-{segment_index}-{event_index:03d}"
            member_ids.append(event_id)
            events.append({
                "event_id": event_id,
                "review_id": f"review-{segment_index}-{event_index}",
                "asin": f"B00SECRET{segment_index}{event_index}",
                "review_date": f"2026-01-{event_index + 1:02d}",
                "review_text": "raw review content must not be included " * 100,
                "scenario": {"journey_action": f"动作 {segment_index}-{event_index}",
                             "context": "使用玩具"},
                "motivation": {"explicit": "安抚狗狗"},
                "task": {"core_job": "让狗狗玩耍"},
                "experience": {"pain": ["容易损坏"]},
                "workaround": {"standardized_action": "监督使用"},
                "evidence_quote": f"短证据 {segment_index}-{event_index}",
            })
        segments.append({
            "segment_id": f"SEG-{segment_index}", "name": f"细分{segment_index}",
            "persona_summary": "画像说明" * 100, "jtbd_statement": "任务说明" * 100,
            "member_event_ids": member_ids,
        })

    payload = build_journey_stage_proposal_input(events, segments)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 192_000
    assert payload["event_count"] == 90
    assert payload["sampling"]["sampled_event_count"] == 6
    assert payload["sampling"]["active_segment_count"] == 3
    assert [row["member_event_count"] for row in payload["segments"]] == [30, 30, 30]
    sample_ids = {signal["event_id"] for segment in payload["segments"]
                  for signal in segment["review_signals"]}
    assert sample_ids == {f"event-{segment_index}-{event_index:03d}"
                          for segment_index in range(3) for event_index in (0, 29)}
    assert "review_text" not in encoded.decode("utf-8")
    assert "B00SECRET" not in encoded.decode("utf-8")
    assert payload == build_journey_stage_proposal_input(events, segments)


def test_mapping_candidates_deduplicate_values_and_keep_expansion_relations_local():
    events = [
        {"event_id": "event-1", "review_id": "review-1", "review_text": "private full review one",
         "experience": {"pain": ["Too loud", "Breaks easily"]}},
        {"event_id": "event-2", "review_id": "review-2", "review_text": "private full review two",
         "experience": {"pain": ["too   loud"]}},
    ]

    candidates = build_mapping_candidates(events)
    loud = next(row for row in candidates if row["raw_value"].casefold() == "too loud")
    assert len(candidates) == 2
    assert {row["event_id"] for row in loud["event_rows"]} == {"event-1", "event-2"}
    assert all(row["candidate_id"] and row["namespace"] == "pain" for row in candidates)

    tags = [
        {"tag_id": "TAG-PAIN", "namespace": "pain", "canonical_name": "声音太大",
         "canonical_name_en": "Excessive noise", "definition": "评论明确描述的声音过大",
         "definition_en": "The sound is described as too loud."},
        {"tag_id": "TAG-TASK", "namespace": "task", "canonical_name": "任务",
         "canonical_name_en": "Task", "definition": "任务", "definition_en": "A user task."},
    ]
    batches = build_mapping_candidate_batches(candidates, tags)
    submitted = [candidate for batch in batches for candidate in batch["payload"]["candidates"]]
    assert len(submitted) == len(candidates)
    assert all("event_rows" not in candidate and "review_text" not in candidate for candidate in submitted)
    assert all({tag["namespace"] for tag in batch["payload"]["tag_dictionary"]} == {"pain"}
               for batch in batches)
    assert "private full review" not in json.dumps(batches, ensure_ascii=False)
    echoed_source = "A complete long review sentence copied into a derived event field " * 3
    projected = pipeline_module.project_model_events([{
        "event_id": "event-echo", "review_id": "review-echo", "review_text": echoed_source,
        "evidence_quote": echoed_source, "fact_inference": echoed_source,
        "experience": {"pain": [echoed_source]},
    }])[0]
    assert "review_text" not in projected
    assert projected["evidence_quote"] == ""
    assert projected["experience"]["pain"] == [""]
    assert "fact_inference" not in projected
    assert "complete long review sentence" not in json.dumps(projected, ensure_ascii=False)
    payload = batches[0]["payload"]
    complete = {"mappings": [{"candidate_id": row["candidate_id"], "tag_id": "TAG-PAIN"}
                              for row in payload["candidates"]]}
    assert pipeline_module._cacheable_stage_result("map_dictionary", payload, complete)
    assert not pipeline_module._cacheable_stage_result(
        "map_dictionary", payload, {"mappings": complete["mappings"] + [complete["mappings"][0]]})


def test_mapping_batches_group_candidates_by_namespace_to_avoid_repeating_irrelevant_tags():
    candidates = [
        {"candidate_id": f"candidate-{namespace}-{index}", "field_path": f"{namespace}.value",
         "raw_value": f"value-{index}", "namespace": namespace}
        for index in range(3) for namespace in ("pain", "task", "scenario")
    ]
    tags = [{"tag_id": f"TAG-{namespace}-{index}", "namespace": namespace,
             "canonical_name": f"canonical-{index}", "canonical_name_en": f"English canonical {index}",
             "definition": "definition " * 10, "definition_en": "English definition " * 10}
            for namespace in ("pain", "task", "scenario") for index in range(4)]

    batches = build_mapping_candidate_batches(candidates, tags, max_candidates=2)
    assert len(batches) == 6
    assert all(len({row["namespace"] for row in batch["candidates"]}) == 1 for batch in batches)
    assert all({tag["namespace"] for tag in batch["payload"]["tag_dictionary"]} ==
               {batch["candidates"][0]["namespace"]} for batch in batches)
    assert all({"canonical_name_en", "definition_en"} <= set(tag)
               for batch in batches for tag in batch["payload"]["tag_dictionary"])
    assert {row["candidate_id"] for batch in batches for row in batch["candidates"]} == {
        row["candidate_id"] for row in candidates}


def test_report_compiler_redacts_full_review_copied_into_derived_fields():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "backend" / "amazon_voc" / "runtime" / "scripts" / "compile_analysis.py"
    spec = importlib.util.spec_from_file_location("amazon_voc_compile_analysis_redaction_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = "The squeaker broke after one short play session."
    payload = {"segments": [{
        "pains": [{"label": source}, {"label": "A shorter safe summary."}],
        "need_priorities": [{"basis": "Verbatim: the squeaker broke\nafter one short play session."}],
        "evidence_snippets": [{"quote": source, "quote_omitted": False}],
    }]}

    redacted = module._redact_full_review_text(payload, [{"review_text": source}])

    assert redacted == 3
    assert source.casefold() not in json.dumps(payload, ensure_ascii=False).casefold()
    assert payload["segments"][0]["evidence_snippets"][0]["quote_omitted"] is True
    assert all("完整评论原文未嵌入报告" in value for value in (
        payload["segments"][0]["pains"][0]["label"],
        payload["segments"][0]["need_priorities"][0]["basis"],
        payload["segments"][0]["evidence_snippets"][0]["quote"],
    ))


def test_safe_provider_preserves_nonretryable_error_and_redacts_secret(voc):
    from backend.amazon_voc.runtime.voc_app.provider import ModelError

    project = voc.create_project({
        "name": "Safe provider error metadata", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)

    class PermanentErrorProvider:
        calls = 0
        def call(self, *args, **kwargs):
            self.calls += 1
            raise ModelError("bad request test-secret-never-persist", retryable=False, status_code=400)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="test-secret-never-persist", model_protocol="chat", extractor_model="fake-text",
        reviewer_model="fake-text", max_repairs=2,
    )
    safe_provider = voc_service_module.SafeProvider(settings)
    inner = PermanentErrorProvider()
    safe_provider.inner = inner
    pipeline = Pipeline(settings, voc.db, voc.storage, safe_provider)

    with pytest.raises(ValueError) as error:
        pipeline._call(voc.db.get_run(run["id"]), 1, workdir, "build_journey", "journey-stages",
                       {"event_count": 0, "segments": [], "event_assignments": [], "fixed_stages": []})

    assert str(error.value).endswith("bad request [redacted]")
    assert inner.calls == 1
    calls = voc.db.all("SELECT status,retries,error FROM model_calls WHERE run_id=? AND stage=?",
                       (run["id"], "build_journey"))
    assert len(calls) == 1
    assert calls[0]["retries"] == 0
    assert "test-secret-never-persist" not in calls[0]["error"]
    with pytest.raises(ModelError) as wrapped:
        safe_provider.call("build_journey", "fake-text", "", {}, {})
    assert wrapped.value.retryable is False
    assert wrapped.value.status_code == 400


def test_oversized_dictionary_uses_bounded_batches_and_merges_same_tags(voc):
    project = voc.create_project({
        "name": "Batched dictionary", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    raw_pain = "The squeaker volume is overwhelming " + ("x" * 4_000)
    events = [{"event_id": f"event-{index:03d}", "review_id": f"review-{index:03d}",
               "review_text": f"original-review-{index}", "evidence_quote": "the squeaker is loud",
               "experience": {"pain": [raw_pain]}}
              for index in range(65)]
    (cache / "events.json").write_text(json.dumps({"events": events}), encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"marketplace": "US", "category": "Pet Toys"}),
                                       encoding="utf-8")

    class BatchedDictionaryProvider:
        def __init__(self):
            self.calls = []
            self.dictionary_payloads = []
        def call(self, task, model, instructions, payload, schema):
            self.calls.append(task)
            if task == "build_dictionary":
                self.dictionary_payloads.append(payload)
                assert pipeline_module._json_size_bytes(payload) <= pipeline_module.MAX_DICTIONARY_MODEL_INPUT_BYTES
                assert all("review_text" not in event for event in payload["events"])
                event_ids = [event["event_id"] for event in payload["events"]]
                return {"tags": [{"tag_id": f"BATCH-TAG-{len(self.dictionary_payloads)}",
                    "namespace": "pain", "canonical_name": "Squeaker volume too loud",
                    "canonical_name_en": "Squeaker volume is too loud",
                    "definition": "扬声器音量过高。", "definition_en": "The squeaker volume is too high.",
                    "parent_tag_id": "", "raw_variants": [raw_pain],
                    "include_examples": ["squeaker volume"], "exclude_examples": [],
                    "support_event_ids": event_ids, "confidence": "高"}]}, {"latency_ms": 1}
            if task == "map_dictionary":
                mapping_payload = payload.get("original_input", payload)
                tag = next(item for item in mapping_payload["tag_dictionary"] if item["namespace"] == "pain")
                return {"mappings": [{"candidate_id": candidate["candidate_id"], "tag_id": tag["tag_id"]}
                    for candidate in mapping_payload["candidates"] if candidate["namespace"] == "pain"]}, {"latency_ms": 1}
            raise AssertionError(task)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        max_repairs=0,
    )
    provider = BatchedDictionaryProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._normalize(voc.db.get_run(run["id"]), 1, workdir)
    dictionary = json.loads((cache / "tag_dictionary.json").read_text(encoding="utf-8"))
    assert len(provider.dictionary_payloads) > 1
    assert dictionary["metadata"]["dictionary_generation_mode"] == "model_batched"
    assert dictionary["metadata"]["dictionary_batch_count"] == len(provider.dictionary_payloads)
    assert dictionary["metadata"]["dictionary_input_bytes"] > pipeline_module.MAX_DICTIONARY_MODEL_INPUT_BYTES
    assert dictionary["metadata"]["mapping_missing_count"] == 0
    normalized_tags = [tag for tag in dictionary["tags"] if tag["normalization_status"] == "normalized"]
    assert len(normalized_tags) == 1
    assert set(normalized_tags[0]["support_event_ids"]) == {event["event_id"] for event in events}
    assert normalized_tags[0]["canonical_name_en"] == "Squeaker volume is too loud"
    assert normalized_tags[0]["definition_en"] == "The squeaker volume is too high."
    pain_mappings = [row for row in dictionary["mappings"] if row["field_path"] == "experience.pain"]
    assert len(pain_mappings) == len(events)
    assert {row["tag_id"] for row in pain_mappings} == {normalized_tags[0]["tag_id"]}
    assert dictionary["metadata"]["normalized_tag_count"] == 1

    call_count = len(provider.calls)
    pipeline._normalize(voc.db.get_run(run["id"]), 1, workdir)
    assert len(provider.calls) == call_count


def test_individually_oversized_dictionary_event_falls_back_locally(voc):
    project = voc.create_project({
        "name": "Single oversized dictionary event", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    event = {"event_id": "event-large", "review_id": "review-large",
             "experience": {"pain": ["x" * 210_000]}}
    (cache / "events.json").write_text(json.dumps({"events": [event]}), encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"marketplace": "US", "category": "Pet Toys"}),
                                       encoding="utf-8")

    class NoCallProvider:
        calls = 0
        def call(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("individually oversized input must remain local")

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        max_repairs=0,
    )
    provider = NoCallProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._normalize(voc.db.get_run(run["id"]), 1, workdir)
    dictionary = json.loads((cache / "tag_dictionary.json").read_text(encoding="utf-8"))
    assert provider.calls == 0
    assert dictionary["metadata"]["dictionary_generation_mode"] == "no_model_input_oversized_event_fallback"
    assert dictionary["metadata"]["dictionary_oversized_event_count"] == 1
    assert dictionary["metadata"]["mapping_oversized_candidate_count"] == 1
    assert dictionary["metadata"]["normalized_tag_count"] == 0
    assert all(row["mapping_method"] == "raw_passthrough" for row in dictionary["mappings"])


def test_segment_proposals_batch_all_profiles_then_merge_duplicate_candidates(voc):
    project = voc.create_project({
        "name": "Segment profile batches", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    events = []
    for index in range(30):
        events.append({"event_id": f"event-{index:03d}", "review_id": f"review-{index:03d}",
            "audience": {"user": f"dog owner {index} " + ("x" * 3_500)}, "scenario": {}, "motivation": {},
            "task": {}, "experience": {}, "workaround": {}, "evidence_quote": ""})
    (cache / "events.json").write_text(json.dumps({"events": events}), encoding="utf-8")
    (cache / "tag_dictionary.json").write_text(json.dumps({"tags": []}), encoding="utf-8")
    (cache / "cleaned_reviews.json").write_text(json.dumps({"rows": [{"review_id": e["review_id"]}
        for e in events]}), encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"analysis_config": {}}), encoding="utf-8")

    class ProposalProvider:
        def __init__(self):
            self.proposals = []
        def call(self, task, model, instructions, payload, schema):
            if task == "propose_segments":
                self.proposals.append(payload)
                return {"segments": [segment_result("SEG-ALL", "狗主人", {
                    "audience": "狗主人", "scenario": "日常", "motivation": "陪伴", "task": "玩耍"}),
                    segment_result("SEG-INACTIVE", "未命中细分", {
                    "audience": "新手犬主", "scenario": "训练", "motivation": "纠正行为", "task": "行为训练"})]}, {"latency_ms": 1}
            if task == "assign_segments":
                return {"assignments": [{"event_id": event["event_id"], "segment_id": "SEG-ALL"}
                    for event in payload["events"]]}, {"latency_ms": 1}
            raise AssertionError(task)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        concurrency=1,
    )
    provider = ProposalProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    pipeline._segment(voc.db.get_run(run["id"]), 1, workdir)
    assert len(provider.proposals) > 1
    assert sum(len(payload["profiles"]) for payload in provider.proposals) == len(events)
    assert sum(payload["coverage"]["event_count"] for payload in provider.proposals) == len(events)
    submitted = {profile["d"][0][0] for payload in provider.proposals for profile in payload["profiles"]}
    assert len(submitted) == len(events)
    model = json.loads((cache / "segment_model.json").read_text(encoding="utf-8"))
    assert len(model["segments"]) == 1
    assert len(model["segments"][0]["member_event_ids"]) == len(events)
    assert model["metadata"]["proposed_segment_count"] == 2
    assert model["metadata"]["inactive_candidate_count"] == 1
    assert model["metadata"]["proposal_mode"] == "profile_batches"
    assert model["metadata"]["proposal_batch_count"] == len(provider.proposals)


def test_single_oversized_segment_profile_is_rejected_before_model_request():
    with pytest.raises(ValueError, match="单个人群画像超过细分提案输入上限"):
        build_segment_proposal_batches({
            "profiles": [{"d": [["x" * 2_000]], "e": 1, "r": 1, "n": [], "q": []}],
            "coverage": {"event_count": 1},
        }, max_bytes=1024)


def test_nonretryable_model_error_stops_pipeline_attempts_immediately(voc):
    from backend.amazon_voc.runtime.voc_app.provider import ModelError

    project = voc.create_project({
        "name": "Permanent provider error", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)

    class PermanentErrorProvider:
        calls = 0
        def call(self, *args, **kwargs):
            self.calls += 1
            raise ModelError("模型接口 HTTP 400", retryable=False, status_code=400)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        max_repairs=2,
    )
    provider = PermanentErrorProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    with pytest.raises(ValueError, match="模型接口 HTTP 400"):
        pipeline._call(voc.db.get_run(run["id"]), 1, workdir, "build_dictionary", "dictionary", {"events": []})
    assert provider.calls == 1


def test_dynamic_assignment_schema_repairs_unknown_segment_id_before_accepting(voc):
    from backend.amazon_voc.runtime.voc_app.schemas import SCHEMAS

    project = voc.create_project({
        "name": "Dynamic assignment ID validation", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)

    class InvalidThenValidProvider:
        def __init__(self):
            self.payloads = []
            self.schemas = []
        def call(self, task, model, instructions, payload, schema):
            self.payloads.append(payload)
            self.schemas.append(schema)
            chosen_id = "OUTSIDE-CANDIDATES" if len(self.payloads) == 1 else "SEG-VALID"
            return {"assignments": [{"event_id": "event-1", "segment_id": chosen_id}]}, {"latency_ms": 1}

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        max_repairs=0,
    )
    provider = InvalidThenValidProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    assignment_schema = json.loads(json.dumps(SCHEMAS["assign_segments"]))
    assignment_schema["properties"]["assignments"]["items"]["properties"]["segment_id"]["enum"] = ["SEG-VALID"]
    result = pipeline._call(voc.db.get_run(run["id"]), 1, workdir, "assign_segments", "strict-coverage",
                            {"events": [{"event_id": "event-1"}], "required_event_ids": ["event-1"]},
                            schema_override=assignment_schema)

    assert len(provider.payloads) == 2
    assert provider.schemas[0]["properties"]["assignments"]["items"]["properties"]["segment_id"]["enum"] == ["SEG-VALID"]
    assert "SEG-VALID" in provider.payloads[1]["repair_instruction"]
    assert result["assignments"][0]["segment_id"] == "SEG-VALID"


def test_segment_retry_reuses_base_batches_and_adds_only_a_new_bounded_repair(voc):
    project = voc.create_project({
        "name": "Stable assignment retry keys", "marketplace": "US", "country": "US",
        "language": "en", "category": "Pet Toys",
    })
    run = voc.create_run(project["id"], "reviews.xlsx", review_workbook())
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)
    cache = workdir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    event = {"event_id": "event-1", "review_id": "review-1", "audience": {"user": "dog owner"},
             "scenario": {}, "motivation": {}, "task": {}, "experience": {}, "workaround": {}, "evidence_quote": ""}
    (cache / "events.json").write_text(json.dumps({"events": [event]}), encoding="utf-8")
    (cache / "tag_dictionary.json").write_text(json.dumps({"tags": []}), encoding="utf-8")
    (cache / "cleaned_reviews.json").write_text(json.dumps({"rows": [{"review_id": "review-1"}]}),
                                                 encoding="utf-8")
    (cache / "master.json").write_text(json.dumps({"analysis_config": {}}), encoding="utf-8")

    class RetryCoverageProvider:
        def __init__(self):
            self.assignment_calls = 0
            self.recovery_mode = False
        def call(self, task, model, instructions, payload, schema):
            if task == "propose_segments":
                return {"segments": [segment_result("SEG-VALID", "狗主人", {
                    "audience": "狗主人", "scenario": "日常", "motivation": "陪伴", "task": "玩耍"})]}, {"latency_ms": 1}
            if task == "assign_segments":
                self.assignment_calls += 1
                event_ids = [row["event_id"] for row in payload.get("events", [])]
                if self.recovery_mode:
                    return {"assignments": [{"event_id": event_id, "segment_id": "SEG-VALID"}
                                            for event_id in event_ids]}, {"latency_ms": 1}
                if self.assignment_calls == 1:
                    return {"assignments": [{"event_id": "event-1", "segment_id": "UNKNOWN"}]}, {"latency_ms": 1}
                return {"assignments": []}, {"latency_ms": 1}
            raise AssertionError(task)

    settings = voc_service_module.Settings(
        data_dir=voc.root, database_path=voc.db.path, model_base_url="https://model.invalid/v1",
        model_api_key="", model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text",
        concurrency=1, max_repairs=0,
    )
    provider = RetryCoverageProvider()
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    with pytest.raises(ValueError, match="细分归属仍缺少 1 个事件"):
        pipeline._segment(voc.db.get_run(run["id"]), 1, workdir)
    first_attempt_calls = provider.assignment_calls
    assert first_attempt_calls == 4  # base + three bounded coverage attempts

    provider.recovery_mode = True
    pipeline._segment(voc.db.get_run(run["id"]), 1, workdir)
    assert provider.assignment_calls == first_attempt_calls + 1
    assignment_batches = voc.db.all(
        "SELECT batch_id,status FROM batches WHERE run_id=? AND revision=? AND stage='assign_segments' ORDER BY batch_id",
        (run["id"], 1))
    assert not any(row["batch_id"].startswith("assignment-r01-") for row in assignment_batches)
    segment_model = json.loads((cache / "segment_model.json").read_text(encoding="utf-8"))
    assert segment_model["segments"][0]["member_event_ids"] == ["event-1"]


def test_voc_stage_cache_reuses_complete_results_and_force_refreshes(tmp_path, voc, monkeypatch):
    monkeypatch.setattr(pipeline_module, "STAGE_RESULT_CACHE",
                        LocalResultCache("voc-stage", root=tmp_path / "cache"))
    project = voc.create_project({"name": "Stage cache", "marketplace": "US", "country": "US",
                                  "language": "en", "category": "Swimwear"})
    payload = {"event_count": 0, "segments": [], "event_assignments": [], "fixed_stages": []}

    class StageProvider:
        calls = 0

        def call(self, task, model, instructions, request, schema):
            self.calls += 1
            assert task == "build_journey"
            return {"stages": [stage_result("STAGE-1", "使用", 1, "执行使用任务")], "assignments": []}, {
                "latency_ms": 1, "input_tokens": 30, "output_tokens": 12,
                "cache_hit_input_tokens": 20, "cache_miss_input_tokens": 10,
            }

    provider = StageProvider()
    settings = voc_service_module.Settings(data_dir=voc.root, database_path=voc.db.path,
        model_base_url="https://model.invalid/v1", model_api_key="",
        model_protocol="chat", extractor_model="fake-text", reviewer_model="fake-text")
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)

    first = voc.db.create_run(project["id"], {"reviews": "first.xlsx", "reviews_sha256": "a"})
    first_workdir = voc.storage.revision_dir(project["id"], first["id"], 1)
    assert pipeline._call(first, 1, first_workdir, "build_journey", "journey-stages", payload)["stages"]

    second = voc.db.create_run(project["id"], {"reviews": "second.xlsx", "reviews_sha256": "b"})
    second_workdir = voc.storage.revision_dir(project["id"], second["id"], 1)
    pipeline._call(second, 1, second_workdir, "build_journey", "journey-stages", payload)
    assert provider.calls == 1
    second_ledger = usage_summary("voc", second["id"], project.get("workspace_id"))
    stage = next(row for row in second_ledger["stages"] if row["stage"] == "build_journey")
    assert stage["local_cache_hits"] == 1
    assert stage["requests"] == 0

    refreshed = voc.db.create_run(project["id"], {"reviews": "refreshed.xlsx", "reviews_sha256": "c",
                                                   "force_refresh": True})
    refresh_workdir = voc.storage.revision_dir(project["id"], refreshed["id"], 1)
    pipeline._call(refreshed, 1, refresh_workdir, "build_journey", "journey-stages", payload)
    assert provider.calls == 2


def test_run_checkpoint_usage_records_the_reviewer_model(tmp_path, voc, monkeypatch):
    monkeypatch.setattr(pipeline_module, "STAGE_RESULT_CACHE",
                        LocalResultCache("voc-stage", root=tmp_path / "cache"))
    project = voc.create_project({"name": "Reviewer ledger", "marketplace": "US", "country": "US",
                                  "language": "en", "category": "Swimwear"})
    payload = {"event_count": 0, "segments": [], "event_assignments": [], "fixed_stages": []}

    class StageProvider:
        calls = 0

        def call(self, task, model, instructions, request, schema):
            self.calls += 1
            assert model == "fake-reviewer"
            return {"stages": [stage_result("STAGE-1", "使用", 1, "执行使用任务")], "assignments": []}, {
                "latency_ms": 1, "input_tokens": 30, "output_tokens": 12,
            }

    provider = StageProvider()
    settings = voc_service_module.Settings(data_dir=voc.root, database_path=voc.db.path,
        model_base_url="https://model.invalid/v1", model_api_key="",
        model_protocol="chat", extractor_model="fake-extractor", reviewer_model="fake-reviewer")
    pipeline = Pipeline(settings, voc.db, voc.storage, provider)
    run = voc.db.create_run(project["id"], {"reviews": "reviewer.xlsx", "reviews_sha256": "reviewer"})
    workdir = voc.storage.revision_dir(project["id"], run["id"], 1)

    pipeline._call(run, 1, workdir, "build_journey", "journey-stages", payload, reviewer=True)
    pipeline._call(run, 1, workdir, "build_journey", "journey-stages", payload, reviewer=True)

    assert provider.calls == 1
    summary = usage_summary("voc", run["id"], project.get("workspace_id"))
    reviewer_stage = next(row for row in summary["stages"]
                          if row["stage"] == "build_journey" and row["model"] == "fake-reviewer")
    assert reviewer_stage["successful_requests"] == 1
    assert reviewer_stage["local_cache_hits"] == 1
    assert not any(row["model"] == "fake-extractor" for row in summary["stages"])
