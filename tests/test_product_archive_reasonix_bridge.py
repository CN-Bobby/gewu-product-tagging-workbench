"""Offline checks for the Product Archive Reasonix CLI bridge."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from backend.product_archive import reasonix_research, service


def test_reasonix_skill_documents_v3_segment_cards_and_validation():
    skill = reasonix_research.SKILL.read_text(encoding="utf-8")
    assert all(field in skill for field in ("schema_version", "segments", "product", "audience",
        "scenarios", "culture", "seasonality", "needs_validation", "evidence_refs", "limits"))
    assert "both the product configuration" in skill
    assert "Do not invent business keywords" in skill
    assert "at least three comparable months" in skill
    assert "Search volume is not product sales" in skill
    assert "最多 5 个细分" not in skill


def test_failed_cli_with_complete_v3_json_is_only_available_to_marked_offline_recovery():
    claim = {"text": "用于儿童水上活动。", "basis": "inference",
             "basis_note": "由产品用途推断", "classification": "hypothesis",
             "evidence_refs": [], "limits": "需核对样本"}
    fields = {key: {"items": [dict(claim)], "missing_reason": ""}
              for key in ("product", "audience", "scenarios", "culture", "seasonality")}
    fields["seasonality"]["items"][0].update(kind="possible_use_period", measured_months=[])
    payload = {"market_analysis": {"schema_version": 3, "overview": claim,
        "segments": [{"id": "S1", "name": "防晒泳衣", "fields": fields,
                      "needs_validation": []}], "needs_validation": []}}
    failed = {"subtype": "error_during_execution", "is_error": True,
              "result": json.dumps(payload, ensure_ascii=False)}
    assert reasonix_research.has_complete_failed_v3_result(failed)
    with pytest.raises(reasonix_research.ResearchRuntimeError):
        reasonix_research.normalize_result(failed, [])
    recovered = reasonix_research.normalize_result(failed, [],
        allow_failed_v3_result=True)
    assert recovered["cli_complete"] is False
    assert recovered["analysis"]["schema_version"] == 3
    assert not reasonix_research.has_complete_failed_v3_result({**failed, "result": "{broken"})
    assert not reasonix_research.has_complete_failed_v3_result({**failed, "result": "{}"})


def _runtime(tmp_path, monkeypatch, *, envelope, ledger_rows=()):
    monkeypatch.setattr(service, "ARCHIVES", tmp_path / "archives")
    home = tmp_path / "runtime-home"
    home.mkdir()
    monkeypatch.setattr(reasonix_research, "environment", lambda *_args, **_kwargs: {
        "REASONIX_HOME": str(home), "TAG_STORAGE": str(tmp_path), "GEWU_PROFILE": "dev"})
    monkeypatch.setattr(reasonix_research, "provider_home", lambda: tmp_path / "providers")
    monkeypatch.setattr(reasonix_research.harness, "executable", lambda: "reasonix-test")
    cleared = []
    monkeypatch.setattr(reasonix_research, "clear_ephemeral_credential", lambda path: cleared.append(path))
    launched = []

    def fake_run(command, **kwargs):
        launched.append((command, kwargs))
        workspace = Path(command[command.index("--dir") + 1])
        (workspace / "web-ledger.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

    monkeypatch.setattr(reasonix_research.subprocess, "run", fake_run)
    return launched, cleared


def test_reasonix_bridge_uses_isolated_skill_and_only_fetched_web_citations(tmp_path, monkeypatch):
    url = "https://example.com/research"
    evidence_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:16]
    analysis = {"segments": [], "product_forms": [], "modules": {}, "needs_validation": []}
    envelope = {"type": "result", "subtype": "success", "is_error": False,
                "result": json.dumps({"research_questions": ["What is the task?"],
                                      "findings": [{"text": "Sourced finding", "evidence_ids": [evidence_id]}],
                                      "analysis": analysis}),
                "usage": {"input_tokens": 50, "output_tokens": 100}, "num_turns": 2}
    rows = [{"kind": "search", "status": "success", "url": url, "evidence_id": evidence_id},
            {"kind": "fetch", "status": "success", "url": url, "evidence_id": evidence_id,
             "title": "Research", "excerpt": "Relevant content", "query": "research query"}]
    launched, cleared = _runtime(tmp_path, monkeypatch, envelope=envelope, ledger_rows=rows)

    result = reasonix_research.run_research("a" * 32, "b" * 32,
        model_ref="deepseek/model", provider="deepseek", credential_id="credential-one",
        brief={"category_context": "test category", "materials": []})

    assert result["analysis"] == analysis
    assert result["citations"] == [{"evidence_id": evidence_id, "url": url,
        "title": "Research", "excerpt": "Relevant content", "publisher": "",
        "retrieved_at": result["citations"][0]["retrieved_at"],
        "query": "research query", "source_type": "public_web"}]
    assert result["findings"] == [{"text": "Sourced finding", "evidence_ids": [evidence_id]}]
    command, kwargs = launched[0]
    assert command[:2] == ["reasonix-test", "run"]
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert kwargs["timeout"] == reasonix_research.TIMEOUT_SECONDS
    workspace = Path(command[command.index("--dir") + 1])
    assert (workspace / ".reasonix/skills/product-archive-research/SKILL.md").is_file()
    assert json.loads((workspace / "reasonix-result.json").read_text(encoding="utf-8"))["usage"] == {
        "input_tokens": 50, "output_tokens": 100}
    recorded = reasonix_research.read_recorded_result("a" * 32, "b" * 32)
    assert recorded is not None
    assert recorded["envelope"]["usage"] == {"input_tokens": 50, "output_tokens": 100}
    assert len(recorded["ledger"]) == 2
    config = (workspace / "reasonix.toml").read_text(encoding="utf-8")
    assert "backend.product_archive.research_mcp" in config
    assert "credential-one" in config
    assert "write_file" not in config
    assert cleared == [str(tmp_path / "runtime-home")]


def test_reasonix_bridge_rejects_incomplete_envelope_and_cleans_secret(tmp_path, monkeypatch):
    launched, cleared = _runtime(tmp_path, monkeypatch, envelope={
        "type": "result", "subtype": "error", "is_error": True, "result": "failed"})
    with pytest.raises(reasonix_research.ResearchRuntimeError, match="Reasonix 研究未完成"):
        reasonix_research.run_research("a" * 32, "b" * 32,
            model_ref="deepseek/model", provider="deepseek", credential_id="credential-one",
            brief={"category_context": "test category"})
    assert launched
    assert cleared == [str(tmp_path / "runtime-home")]


def test_reasonix_bridge_accepts_separate_question_and_analysis_json_messages():
    payload = (json.dumps({"research_questions": ["Which product form?"]}) + "\n"
               + json.dumps({"findings": [], "analysis": {"segments": [
                   {"id": "S1", "name": "Core", "description": "Core task"}],
                   "product_forms": [], "modules": {}, "needs_validation": []}}))
    result = reasonix_research.normalize_result(
        {"subtype": "success", "is_error": False, "result": payload}, [])
    assert result["questions"] == ["Which product form?"]
    assert result["analysis"]["segments"][0]["id"] == "S1"

    duplicate = json.dumps({"analysis": {}}) + "\n" + json.dumps({"analysis": {}})
    with pytest.raises(ValueError):
        reasonix_research.normalize_result(
            {"subtype": "success", "is_error": False, "result": duplicate}, [])


def test_reasonix_bridge_repairs_only_schema_boundary_and_preserves_validation_items():
    modules = {field: [] for field in (
        "product_name", "product_function", "usage_scenarios", "target_audience",
        "buying_motives", "cultural_context", "seasonality")}
    payload = {"research_questions": [], "findings": [], "analysis": {
        "segments": [], "product_forms": [], "modules": modules,
        "needs_validation": [{"segment_id": "", "text": "Check demand"}]}}
    original = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    malformed = original.replace('},"needs_validation":', ',"needs_validation":', 1)

    result = reasonix_research.normalize_result(
        {"subtype": "success", "is_error": False, "result": malformed}, [])

    assert result["analysis"] == payload["analysis"]
    assert result["analysis"]["needs_validation"][0]["text"] == "Check demand"
    assert "needs_validation" not in result["analysis"]["modules"]
    assert result["parse_repair"]["method"] == "single_missing_object_closure"
    assert result["parse_repair"]["location"] == "analysis.modules_before_needs_validation"
    assert result["parse_repair"]["original_sha256"] != result["parse_repair"]["repaired_sha256"]


def test_reasonix_bridge_does_not_guess_when_structure_remains_ambiguous():
    malformed = '{"analysis":{"segments":[],"product_forms":[],"modules":{"product_name":[]}}'
    with pytest.raises(ValueError):
        reasonix_research.normalize_result(
            {"subtype": "success", "is_error": False, "result": malformed}, [])


def test_findings_can_use_selected_local_evidence_but_not_unread_web_hits():
    payload = {"analysis": {}, "findings": [
        {"text": "Local feature", "evidence_ids": ["artifact:tag"]},
        {"text": "Search snippet only", "evidence_ids": ["web:unread"]}]}
    result = reasonix_research.normalize_result(
        {"subtype": "success", "is_error": False, "result": json.dumps(payload)}, [],
        local_evidence_ids={"artifact:tag"})
    assert result["findings"] == [{"text": "Local feature", "evidence_ids": ["artifact:tag"]}]


def test_worker_passes_verified_image_observations_to_text_reasonix_without_raw_images(monkeypatch):
    from backend import usage
    from backend.product_archive import image_observation, overview_drafts as overview, reasonix_worker

    monkeypatch.setattr(service, "_read", lambda _archive_id: {
        "id": "a" * 32, "dataset_id": "dataset-one", "marketplace": "US"})
    monkeypatch.setattr(overview, "_set_phase", lambda *_args: None)
    monkeypatch.setattr(overview, "_selected_sources", lambda *_args: (
        [{"id": "artifact:tag", "selected": True}],
        [{"source_id": "artifact:tag", "type": "tagged_workbook",
          "content": {"rows": 1}, "image_paths": ["selected-image.jpg"]}], []))
    monkeypatch.setattr(overview, "_completed_keyword_material", lambda *_args: {
        "keyword_evidence": []})
    monkeypatch.setattr(overview, "_compact_sample", lambda *_args, **_kwargs: {"rows": 1})
    monkeypatch.setattr(overview, "_comparable_demand_months", lambda *_args: [])
    monkeypatch.setattr(overview, "_fit_prompt_to_char_budget", lambda brief, _limit: (brief, False, 100))
    monkeypatch.setattr(overview, "_save", lambda *_args: None)
    monkeypatch.setattr(overview, "_build_analysis", lambda *_args: (
        {"product_function": [{"text": "Verified draft"}],
         "seasonality": [{"text": "Warm-weather usage is plausible", "basis": "inference"}]},
        {"filtered": [], "completion_blockers": []}))
    finished = []
    monkeypatch.setattr(overview, "_finish", lambda _aid, _rid, row: finished.append(row))
    monkeypatch.setattr(usage, "start_usage_event", lambda **_kwargs: None)
    monkeypatch.setattr(usage, "finish_usage_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(usage, "estimate_text_tokens", lambda _value: 100)
    vision_calls = []
    monkeypatch.setattr(image_observation, "observe", lambda paths, **kwargs: (
        vision_calls.append((paths, kwargs)) or {
            "observations": [{"image_index": 1, "visible_features": "Long sleeves"}],
            "image_count": 1, "usage": {"prompt_tokens": 20}, "issue": ""}))
    agent_calls = []
    monkeypatch.setattr(reasonix_research, "run_research", lambda *_args, **kwargs: (
        agent_calls.append(kwargs) or {
            "analysis": {}, "citations": [], "ledger": [], "questions": [],
            "findings": [], "usage": {"input_tokens": 50, "output_tokens": 80},
            "num_turns": 1, "total_cost": None, "currency": None,
            "image_count": 0, "image_issues": []}))
    row = {"category_context": "girls sun shirt", "data_month_context": "2026-05",
           "selected_source_ids": ["artifact:tag"], "model_snapshot": {
               "available": True, "uses_vision": True,
               "model_ref": "vision/vision-model", "provider": "vision",
               "credential_id": "vision-key", "reasoning_model_ref": "text/text-model",
               "reasoning_provider": "text", "reasoning_credential_id": "text-key"}}

    reasonix_worker.run("a" * 32, "b" * 32, row)

    assert vision_calls[0][0] == ["selected-image.jpg"]
    assert agent_calls[0]["model_ref"] == "text/text-model"
    assert agent_calls[0]["image_paths"] == []
    assert agent_calls[0]["brief"]["image_observations"]["images"][0]["visible_features"] == "Long sleeves"
    assert finished[0]["status"] == "ready"
    assert finished[0]["usage"]["image_count"] == 1
    assert finished[0]["needs_validation"]["seasonality"]["sales_peak_allowed"] is False


def test_worker_passes_selected_keyword_months_as_search_demand_not_sales(monkeypatch):
    from backend import usage
    from backend.product_archive import keyword_seasonality, overview_drafts as overview, reasonix_worker

    monkeypatch.setattr(service, "_read", lambda _archive_id: {
        "id": "a" * 32, "dataset_id": "dataset-one", "workspace_id": "workspace-one", "marketplace": "US"})
    monkeypatch.setattr(overview, "_set_phase", lambda *_args: None)
    monkeypatch.setattr(overview, "_selected_sources", lambda *_args: (
        [{"id": "artifact:keywords", "selected": True}],
        [{"source_id": "artifact:keywords", "type": "keywords", "content": {"rows": 1},
          "task_run_id": "completed-task"}], []))
    monkeypatch.setattr(overview, "_completed_keyword_material", lambda *_args: {"keyword_evidence": []})
    monkeypatch.setattr(overview, "_comparable_demand_months", lambda *_args: [])
    monkeypatch.setattr(overview, "_fit_prompt_to_char_budget", lambda brief, _limit: (brief, False, 100))
    monkeypatch.setattr(overview, "_save", lambda *_args: None)
    monkeypatch.setattr(overview, "_build_analysis", lambda *_args: ({}, {"filtered": [],
        "completion_blockers": [], "market_analysis": {"schema_version": 3, "segments": []}}))
    finished = []
    monkeypatch.setattr(overview, "_finish", lambda _aid, _rid, value: finished.append(value))
    monkeypatch.setattr(usage, "start_usage_event", lambda **_kwargs: None)
    monkeypatch.setattr(usage, "finish_usage_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(usage, "estimate_text_tokens", lambda _value: 100)
    monkeypatch.setattr(keyword_seasonality, "from_selected_materials", lambda *_args: {
        "status": "available", "source_id": "artifact:keywords", "months": ["2025-01", "2025-02", "2025-03"],
        "keywords": [{"keyword": "work pants for men", "values": [100, 120, 180]}]})
    agent_calls = []
    monkeypatch.setattr(reasonix_research, "run_research", lambda *_args, **kwargs: (
        agent_calls.append(kwargs) or {"analysis": {}, "citations": [], "ledger": [], "questions": [],
            "findings": [], "usage": {}, "num_turns": 1, "total_cost": None, "currency": None,
            "image_count": 0, "image_issues": []}))
    row = {"category_context": "men's pants", "data_month_context": "2026-10",
           "selected_source_ids": ["artifact:keywords"], "model_snapshot": {"available": True,
               "uses_vision": False, "reasoning_model_ref": "text/model",
               "reasoning_provider": "text", "reasoning_credential_id": "fake"}}

    reasonix_worker.run("a" * 32, "b" * 32, row)

    brief = agent_calls[0]["brief"]
    assert brief["keyword_monthly_trends"]["source_id"] == "artifact:keywords"
    assert brief["keyword_monthly_trends"]["metric"] == "monthly_searches"
    assert brief["comparable_demand_months"] == ["2025-01", "2025-02", "2025-03"]
    assert finished[0]["status"] == "ready"


def test_worker_keeps_measured_cli_usage_when_saved_business_result_cannot_parse(tmp_path, monkeypatch):
    from backend import usage
    from backend.product_archive import overview_drafts as overview, reasonix_worker

    archive_id, run_id = "a" * 32, "b" * 32
    monkeypatch.setattr(service, "ARCHIVES", tmp_path / "archives")
    monkeypatch.setattr(service, "_read", lambda _archive_id: {
        "id": archive_id, "marketplace": "US", "dataset_id": "dataset-one"})
    monkeypatch.setattr(overview, "_set_phase", lambda *_args: None)
    monkeypatch.setattr(overview, "_selected_sources", lambda *_args: ([], [], []))
    monkeypatch.setattr(overview, "_completed_keyword_material", lambda *_args: {"keyword_evidence": []})
    monkeypatch.setattr(overview, "_comparable_demand_months", lambda *_args: [])
    monkeypatch.setattr(overview, "_fit_prompt_to_char_budget", lambda brief, _limit: (brief, False, 100))
    monkeypatch.setattr(overview, "_save", lambda *_args: None)
    finished = []
    monkeypatch.setattr(overview, "_finish", lambda _aid, _rid, row: finished.append(row))
    monkeypatch.setattr(usage, "start_usage_event", lambda **_kwargs: "event-one")
    events = []
    monkeypatch.setattr(usage, "finish_usage_event", lambda *_args, **kwargs: events.append(kwargs))
    monkeypatch.setattr(usage, "estimate_text_tokens", lambda _value: 100)

    envelope = {"subtype": "success", "is_error": False, "result": "{broken-json",
                "usage": {"input_tokens": 400, "output_tokens": 50,
                          "cache_read_input_tokens": 300},
                "total_cost": 0.01, "currency": "USD", "num_turns": 2}

    def failed_research(*_args, **_kwargs):
        workspace = reasonix_research._workspace(archive_id, run_id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "reasonix-result.json").write_text(json.dumps(envelope), encoding="utf-8")
        raise overview.ModelOutputError("invalid JSON")

    monkeypatch.setattr(reasonix_research, "run_research", failed_research)
    row = {"category_context": "mens casual pants", "data_month_context": "2026-10",
           "selected_source_ids": [], "model_snapshot": {"available": True,
               "uses_vision": False, "reasoning_model_ref": "deepseek/text",
               "reasoning_provider": "deepseek", "reasoning_credential_id": "text-key"}}

    reasonix_worker.run(archive_id, run_id, row)

    assert finished[0]["status"] == "failed"
    assert finished[0]["usage"]["input_tokens"] == 400
    assert finished[0]["usage"]["cost_amount"] == 0.01
    assert finished[0]["raw_result_available"] is True
    assert events[-1]["status"] == "failed"
    assert events[-1]["usage"]["output_tokens"] == 50


def test_failed_run_recovers_saved_analysis_once_without_model_or_web_calls(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts as overview, reasonix_worker

    archive_id, run_id = "a" * 32, "b" * 32
    archive = {"id": archive_id, "dataset_id": "dataset-one", "workspace_id": "workspace-one",
               "source_snapshot": {}, "integrations": {}, "overview_suggestions": {}}
    monkeypatch.setattr(service, "ARCHIVES", tmp_path / "archives")
    monkeypatch.setattr(service, "_read", lambda _archive_id: archive)
    monkeypatch.setattr(service, "_save", lambda _archive: None)
    source = {"id": "artifact:tag", "type": "tagged_workbook", "available": True,
              "name": "Data-X.xlsx", "status": "completed"}
    monkeypatch.setattr(overview, "list_sources", lambda _archive_id: [source])
    monkeypatch.setattr(overview, "_selected_source_fingerprint", lambda *_args, **_kwargs: "source-hash")
    monkeypatch.setattr(overview, "_selected_sources", lambda *_args: (
        [{**source, "selected": True}],
        [{"source_id": source["id"], "type": "tagged_workbook", "content": {"rows": 1}}], []))
    monkeypatch.setattr(overview, "_completed_keyword_material", lambda *_args: {"keyword_evidence": []})
    monkeypatch.setattr(overview, "_comparable_demand_months", lambda *_args: [])
    monkeypatch.setattr(reasonix_research, "run_research", lambda *_args, **_kwargs: pytest.fail("model called"))
    reconciled = []
    monkeypatch.setattr(reasonix_worker, "reconcile_recorded_usage",
                        lambda *_args: reconciled.append(True))

    snapshot = {"model_ref": "deepseek/vision", "provider": "deepseek",
                "credential_id": "vision-credential", "reasoning_model_ref": "deepseek/text",
                "reasoning_provider": "deepseek", "reasoning_credential_id": "text-credential",
                "uses_vision": True, "available": True}
    identity = {key: snapshot.get(key) for key in (
        "model_ref", "provider", "credential_id", "reasoning_model_ref",
        "reasoning_provider", "reasoning_credential_id", "uses_vision", "available")}
    fingerprint = overview._evidence_fingerprint(archive)
    request_key = overview._request_key(archive_id, "mens casual pants", "2026-10", True,
        [], [source["id"]], fingerprint, "source-hash", identity,
        analysis_version="market-segments-v5-reasonix")
    row = {"id": run_id, "archive_id": archive_id, "status": "failed",
           "analysis_version": "market-segments-v5-reasonix", "research_engine": "reasonix_cli_skill",
           "request_key": request_key, "category_context": "mens casual pants",
           "data_month_context": "2026-10", "public_research": True, "source_urls": [],
           "selected_source_ids": [source["id"]], "evidence_fingerprint": fingerprint,
           "selected_source_fingerprint": "source-hash", "model_snapshot": snapshot,
           "source_manifest": [], "source_bundles": [], "model_call_ledger": [],
           "suggestions": {}, "usage": None}
    overview._save(archive_id, run_id, row)
    modules = {field: [{"segment_id": "S1", "text": "A specific buyer task and product response.",
                       "basis": "inference", "basis_note": "Product form implies this use.",
                       "evidence_refs": [], "limits": "Needs buyer validation."}]
               for field in overview.OVERVIEW_FIELDS}
    analysis = {"segments": [{"id": "S1", "name": "Core", "description": "Core task",
                              "classification": "hypothesis", "evidence_refs": []}],
                "product_forms": [{"id": "F1", "name": "Casual pants",
                                   "description": "A distinct product form"}],
                "modules": modules, "needs_validation": [{"segment_id": "S1", "text": "Check fit"}]}
    envelope = {"subtype": "success", "is_error": False,
                "result": json.dumps({"research_questions": [], "findings": [], "analysis": analysis}),
                "usage": {"input_tokens": 100, "output_tokens": 30},
                "total_cost": 0.002, "currency": "USD", "num_turns": 1}
    workspace = reasonix_research._workspace(archive_id, run_id)
    workspace.mkdir(parents=True)
    (workspace / "reasonix-result.json").write_text(json.dumps(envelope), encoding="utf-8")

    recovered = overview.recover(archive_id, run_id)
    repeated = overview.recover(archive_id, run_id)

    assert recovered["status"] == repeated["status"] == "ready"
    assert recovered["recovered_from_saved_result"] is True
    assert all(recovered["suggestions"].get(field) for field in overview.OVERVIEW_FIELDS)
    assert recovered["diagnostics"]["needs_validation"][0]["text"] == "Check fit"
    assert recovered["usage"]["input_tokens"] == 100
    assert len(archive["overview_suggestions"]["product_function"]) == 1
    assert len(reconciled) == 1


def test_recovery_refuses_changed_selected_material_without_model_call(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts as overview

    archive_id, run_id = "a" * 32, "b" * 32
    monkeypatch.setattr(service, "ARCHIVES", tmp_path / "archives")
    monkeypatch.setattr(service, "_read", lambda _archive_id: {"id": archive_id})
    monkeypatch.setattr(overview, "list_sources", lambda _archive_id: [
        {"id": "artifact:tag", "available": True}])
    monkeypatch.setattr(overview, "_selected_source_fingerprint",
                        lambda *_args, **_kwargs: "new-material-hash")
    row = {"id": run_id, "archive_id": archive_id, "status": "failed",
           "analysis_version": "market-segments-v5-reasonix",
           "research_engine": "reasonix_cli_skill", "selected_source_ids": ["artifact:tag"],
           "selected_source_fingerprint": "old-material-hash",
           "evidence_fingerprint": overview._evidence_fingerprint({"id": archive_id})}
    overview._save(archive_id, run_id, row)
    workspace = reasonix_research._workspace(archive_id, run_id)
    workspace.mkdir(parents=True)
    (workspace / "reasonix-result.json").write_text(
        json.dumps({"subtype": "success", "is_error": False, "result": "{}"}), encoding="utf-8")

    state = overview.recovery_status(archive_id, row)
    assert state["available"] is False and "变化" in state["reason"]
    with pytest.raises(ValueError, match="变化"):
        overview.recover(archive_id, run_id)


def test_direct_archive_visit_finds_latest_open_run_and_skips_decided_suggestions(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts as overview

    archive_id = "a" * 32
    monkeypatch.setattr(service, "ARCHIVES", tmp_path / "archives")
    archive = {"id": archive_id, "overview_suggestions": {
        "product_function": [{"id": "pending-one"}]}}
    monkeypatch.setattr(service, "_read", lambda _archive_id: archive)
    directory = tmp_path / "overview-drafts" / archive_id
    directory.mkdir(parents=True)
    rows = [
        {"id": "1" * 32, "status": "ready", "created_at": "2026-10-23T00:01:00+00:00",
         "suggestions": {"product_function": [{"id": "pending-one"}]}},
        {"id": "2" * 32, "status": "ready", "created_at": "2026-10-23T00:02:00+00:00",
         "suggestions": {"product_function": [{"id": "already-decided"}]}},
        {"id": "3" * 32, "status": "failed", "created_at": "2026-10-23T00:03:00+00:00",
         "suggestions": {}},
    ]
    for row in rows:
        (directory / f"{row['id']}.json").write_text(
            json.dumps({**row, "archive_id": archive_id}), encoding="utf-8")

    assert overview.latest_open(archive_id) == {"id": "3" * 32, "status": "failed"}
    (directory / f"{'3' * 32}.json").unlink()
    assert overview.latest_open(archive_id) == {"id": "1" * 32, "status": "ready"}
    archive["overview_suggestions"].clear()
    assert overview.latest_open(archive_id) == {"id": None, "status": None}
