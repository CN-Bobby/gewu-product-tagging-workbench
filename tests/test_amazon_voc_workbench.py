"""Phase 2 integration contract for the single-workbook Amazon VOC workbench page."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from backend.amazon_voc import router as voc_router
from backend.amazon_voc.service import AmazonVOCService


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


class DeterministicVOCProvider:
    """Small schema-compatible fake; keeps integration tests independent of paid APIs."""
    def call(self, task, model, instructions, payload, schema):
        from backend.amazon_voc.runtime.voc_app.quality import iter_values

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
                 "definition": f"样本中的{namespace}表达", "parent_tag_id": "",
                 "raw_variants": [raw], "include_examples": [raw], "exclude_examples": [],
                 "support_event_ids": sorted(set(event_ids)), "confidence": "高"}
                for i, ((namespace, raw), event_ids) in enumerate(sorted(grouped.items()), 1)
            ]}
        elif task == "map_dictionary":
            lookup = {(item["namespace"], item["canonical_name"]): item["tag_id"]
                      for item in payload["tag_dictionary"]}
            result = {"mappings": [
                {"event_id": event["event_id"], "field_path": value["field_path"],
                 "raw_value": value["raw_value"],
                 "tag_id": lookup[(value["namespace"], value["raw_value"])]}
                for event in payload["events"] for value in event["values"]
            ]}
        elif task == "propose_segments":
            result = {"segments": [{"segment_id": "SEG-DAILY", "name": "日常稳定任务者",
                "segment_key": {"audience": "明确使用者", "scenario": "日常使用",
                                "motivation": "完成任务", "task": "稳定完成任务"},
                "persona_summary": "在日常使用中关注任务稳定完成。",
                "jtbd_statement": "明确使用者在日常使用时，为完成任务而稳定完成任务。",
                "merge_reason": "任务与成功标准一致。", "split_reason": "当前没有稳定的相反约束。",
                "need_priorities": []}]}
        elif task == "assign_segments":
            result = {"assignments": [{"event_id": event["event_id"], "segment_id": "SEG-DAILY",
                                       "reason": "四维一致"} for event in payload["events"]]}
        elif task == "build_journey":
            result = {"stages": [{"stage_id": "J-USE", "name": "执行任务", "order": 1,
                                  "definition": "执行核心动作并获得结果"}],
                      "assignments": [{"event_id": event["event_id"], "stage_id": "J-USE",
                                       "reason": "动作发生于执行阶段"}
                                      for event in payload["events"]]}
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
    assert set(run["sources"]) == {"reviews", "reviews_sha256"}
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
    assert html_text.count('class="module"') == 6
    assert "Alexa" not in html_text
    from io import BytesIO
    workbook = load_workbook(BytesIO(excel.content), read_only=True)
    assert len(workbook.sheetnames) == 12
    assert "外部补全" not in workbook.sheetnames
    assert "AI产品建议" not in workbook.sheetnames
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
