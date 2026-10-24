import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import app
from backend.db import Asset, Product, Run, Schema, Session, Snapshot, Tag
from backend.product_archive.analytics import compute_metrics
from backend.product_archive.fields import default_mapping
from backend.product_archive import service as archive_service


def _workbook():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ASIN", "品牌", "月销量", "销售额", "价格", "卖家国家", "配送方式", "评分数", "评分", "上架时间"])
    sheet.append(["ITEM-A", "Alpha", 100, 1000, 10, "US", "FBA", 80, 4.6, "2025-12-01"])
    sheet.append(["ITEM-A", "Alpha", 50, None, 20, "US", "FBA", 120, 4.3, "2025-07-01"])
    sheet.append(["ITEM-B", "Beta", 40, None, 10, "CN", "FBM", 300, 3.8, "2024-09-01"])
    sheet.append(["ITEM-C", "Gamma", 30, 300, 10, "US", "AMZ", 1000, 4.1, "2023-01-01"])
    sheet.append(["ITEM-D", "Delta", 20, 200, 10, None, "FBA", None, None, None])
    sheet.append(["ITEM-E", None, None, 100, 5, "US", "FBA", 40, 4.8, "2026-08-01"])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _upload(client):
    response = client.post("/api/product-archive/import", data={
        "marketplace": "US", "category": "Sample Category", "data_month": "2026-09",
    }, files={"file": ("sample.xlsx", _workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert response.status_code == 200, response.text
    return response.json()


def test_sellersprite_seller_location_header_maps_to_country_distribution():
    mapping = default_mapping(["ASIN", "月销量", "卖家所属地"])
    assert mapping["seller_country"] == "卖家所属地"
    metrics = compute_metrics([
        {"raw": {"ASIN": "A", "月销量": 10, "卖家所属地": "CN"}},
        {"raw": {"ASIN": "B", "月销量": 20, "卖家所属地": "US"}},
        {"raw": {"ASIN": "C", "月销量": 30, "卖家所属地": ""}},
    ], mapping)
    distribution = metrics["competition"]["seller_country"]
    assert distribution["available"] is True
    assert distribution["missing"]["rows"] == 1
    assert distribution["missing"]["units"] == 30


def test_archive_import_mapping_metrics_and_read_only_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        assert archive["dataset"]["count"] == 6
        assert archive["metrics"] is None
        assert "text_job_id" not in archive

        dataset_id = archive["dataset_id"]
        with Session() as session:
            before = session.scalars(select(Product).where(Product.dataset_id == dataset_id).order_by(Product.row)).all()
            original = [dict(row.raw) for row in before]

        confirmed = client.put(f"/api/product-archive/archives/{archive['id']}/mapping",
                               json={"mapping": archive["mapping"]})
        assert confirmed.status_code == 200, confirmed.text
        result = confirmed.json()
        metrics = result["metrics"]
        assert result["mapping_confirmed"] is True
        assert metrics["sample_rows"] == 6
        assert metrics["analysis_rows"] == 5
        assert metrics["duplicate_asins_removed"] == 1
        assert metrics["market_size"]["sample_monthly_sales"] == 190
        assert metrics["unknown_sales_rows"] == 1
        assert metrics["revenue"]["amount"] == 2000
        assert metrics["revenue"]["basis"] == "mixed_source_and_estimate"
        assert metrics["competition"]["asin"]["unit_concentration"]["cr5"]["share"] == 1
        assert metrics["competition"]["asin"]["top_by_units"][0]["name"] == "ITEM-A"
        assert metrics["competition"]["seller_country"]["missing"]["rows"] == 1
        assert metrics["competition"]["seller_country"]["missing"]["units"] == 20
        assert metrics["competition"]["fulfillment"]["available"] is True
        assert metrics["competition"]["listing_age"]["available"] is True
        assert metrics["market_size"]["keyword_search_volume"] is None

        preview = client.get(f"/api/product-archive/archives/{archive['id']}/samples?query=ITEM-A")
        assert preview.status_code == 200
        assert preview.json()["total"] == 2
        assert preview.json()["duplicate_rows"] == 1
        assert preview.json()["duplicate_identifiers"] == 1
        with Session() as session:
            after = session.scalars(select(Product).where(Product.dataset_id == dataset_id).order_by(Product.row)).all()
            assert [dict(row.raw) for row in after] == original


def test_archive_rejects_mapping_columns_outside_source_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        mapping = dict(archive["mapping"])
        mapping["monthly_sales"] = "not an original column"
        response = client.put(f"/api/product-archive/archives/{archive['id']}/mapping", json={"mapping": mapping})
        assert response.status_code == 400
        assert "原始表头" in response.json()["detail"]


def test_archive_can_link_an_existing_dataset_without_copying_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        from backend.importer import import_file
        second = import_file(_workbook(), "existing_dataset.xlsx")
        response = client.post(f"/api/product-archive/archives/{archive['id']}/dataset",
                               json={"dataset_id": second["id"]})
        assert response.status_code == 200, response.text
        linked = response.json()
        assert linked["dataset_id"] == second["id"]
        assert linked["mapping_confirmed"] is False
        assert linked["metrics"] is None
        preview = client.get(f"/api/product-archive/archives/{archive['id']}/samples")
        assert preview.json()["dataset"]["id"] == second["id"]
        assert preview.json()["total"] == 6


def test_archive_overview_is_saved_separately_from_source_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        response = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "overview": "ignored", "product_name": "Sample product", "representative_keywords": "word one\nword two",
            "product_function": "Keeps an item dry", "target_audience": "Outdoor users",
            "cultural_context": "US summer", "seasonality": "May to August",
        })
        assert response.status_code == 200
        overview = response.json()["overview"]
        assert overview["product_name"] == "Sample product"
        assert overview["representative_keywords"] == "word one\nword two"


def test_archive_accepts_suggestion_and_rejects_stale_write(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        record = archive_service._read(archive["id"])
        record["overview_suggestions"] = {"product_function": [{
            "id": "suggestion-1", "text": "Keeps items dry", "source_type": "amazon_voc",
            "source_ids": ["voc-1"], "scope": {"analysis_scope": "VOC"}, "links": [],
            "updated_at": "2026-10-18T00:00:00Z"}], "target_audience": [{
            "id": "suggestion-2", "text": "Outdoor families", "source_type": "amazon_voc",
            "source_ids": ["voc-2"], "scope": {}, "links": [], "updated_at": "2026-10-18T00:00:00Z"}]}
        archive_service._save(record)
        current = archive_service._read(archive["id"])["updated_at"]
        accepted = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "accepted_suggestion_ids": ["suggestion-1"], "ignored_suggestion_ids": ["suggestion-2"],
            "expected_updated_at": current,
        })
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["overview"]["product_function"] == "Keeps items dry"
        assert accepted.json()["overview_sources"]["product_function"]["source_type"] == "amazon_voc"
        assert accepted.json()["overview_sources"]["product_function"]["source_ids"] == ["voc-1"]
        refreshed = client.get(f"/api/product-archive/archives/{archive['id']}").json()
        assert "product_function" not in refreshed["overview_suggestions"]
        assert "target_audience" not in refreshed["overview_suggestions"]
        stale = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "product_function": "Edited", "expected_updated_at": current,
        })
        assert stale.status_code == 400


def test_archive_persists_editable_overview_modules_and_unified_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        modules = [
            {"id": "function", "title": "产品功能分析", "field_key": "product_function",
             "items": [{"id": "function-item", "text": "综合叙述", "source_type": "overview_draft",
                        "source_ids": ["evidence-1"]}]},
            {"id": "audience-custom", "title": "细分人群", "field_key": "target_audience",
             "items": [{"id": "audience-item", "text": "家庭用户", "source_type": "overview_draft",
                        "source_ids": ["evidence-2"]}]},
            {"id": "custom-a", "title": "自定义模块", "field_key": None,
             "items": [{"id": "custom-item", "text": "用户编辑内容", "source_type": "manual",
                        "source_ids": []}]},
        ]
        record = archive_service._read(archive["id"])
        record["overview_suggestions"] = {
            "product_function": [modules[0]["items"][0]],
            "target_audience": [modules[1]["items"][0]],
        }
        archive_service._save(record)
        current = client.get(f"/api/product-archive/archives/{archive['id']}").json()
        response = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "overview_modules": modules,
            "accepted_suggestion_ids": ["function-item"],
            "ignored_suggestion_ids": ["audience-item"],
            "expected_updated_at": current["updated_at"],
        })
        assert response.status_code == 200, response.text
        assert response.json()["overview_modules"] == modules
        assert response.json()["overview_modules_initialized"] is True
        assert response.json()["overview"]["product_function"] == "综合叙述"
        assert "product_function" not in response.json()["overview_suggestions"]
        assert "target_audience" not in response.json()["overview_suggestions"]


def test_archive_distinguishes_explicitly_cleared_modules_from_new_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        assert archive["overview_modules"] == []
        assert not archive.get("overview_modules_initialized")
        cleared = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "overview_modules": [], "expected_updated_at": archive["updated_at"],
        })
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["overview_modules"] == []
        assert cleared.json()["overview_modules_initialized"] is True


def test_archive_rejects_model_generated_keyword_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        response = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "overview_modules": [{"id": "keywords", "title": "代表关键词",
                                  "field_key": "representative_keywords",
                                  "items": [{"id": "model-keyword", "text": "猜测词",
                                             "source_type": "overview_draft"}]}],
        })
        assert response.status_code == 400
        assert "关键词" in response.json()["detail"]


def test_archive_rejects_unknown_overview_module_field_key(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        response = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "overview_modules": [{"id": "custom-a", "title": "自定义模块", "field_key": "unknown_field",
                                  "items": []}],
        })
        assert response.status_code == 400


def test_overview_draft_endpoints_return_persistent_status(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(overview_drafts, "_launch", lambda _archive_id, _run_id: None)
    monkeypatch.setattr(overview_drafts, "list_sources", lambda _archive_id: [{
        "id": "artifact:tagged", "available": True, "selected_by_default": True}])
    with TestClient(app) as client:
        archive = _upload(client)
        body = {"category_context": "女童防晒服", "data_month_context": "2026-06", "local_only": False}
        first = client.post(f"/api/product-archive/archives/{archive['id']}/overview-drafts", json=body)
        second = client.post(f"/api/product-archive/archives/{archive['id']}/overview-drafts", json=body)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()
        current = client.get(f"/api/product-archive/archives/{archive['id']}/overview-drafts/{first.json()['id']}")
        assert current.status_code == 200
        assert current.json()["status"] == "running"
        assert "request_key" not in current.json()


def test_overview_draft_rejects_web_inputs_and_keeps_local_request_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(overview_drafts, "_launch", lambda _archive_id, _run_id: None)
    monkeypatch.setattr(overview_drafts, "list_sources", lambda _archive_id: [{
        "id": "artifact:tagged", "available": True, "selected_by_default": True}])
    with TestClient(app) as client:
        archive = _upload(client)
        path = f"/api/product-archive/archives/{archive['id']}/overview-drafts"
        base = {"category_context": "女童防晒服", "data_month_context": "2026-06", "local_only": True}
        first = client.post(path, json=base)
        repeated = client.post(path, json={**base, "source_urls": []})
        invalid = client.post(path, json={**base, "source_urls": ["https://www.fda.gov/"]})
        assert first.status_code == 200
        assert repeated.json() == first.json()
        assert invalid.status_code == 400
        assert "四类业务产物" in invalid.json()["detail"]


def test_overview_draft_rejects_source_ids_outside_archive_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(overview_drafts, "_launch", lambda _archive_id, _run_id: None)
    with TestClient(app) as client:
        archive = _upload(client)
        response = client.post(f"/api/product-archive/archives/{archive['id']}/overview-drafts", json={
            "category_context": "女童防晒服", "data_month_context": "2026-05",
            "local_only": True, "selected_source_ids": ["artifact:other-workspace-file"],
        })
        assert response.status_code == 400
        assert "不属于当前档案" in response.json()["detail"]


def test_explicit_source_url_requires_fetched_relevant_page_body(monkeypatch):
    from backend import external_effects, knowledge
    from backend.product_archive import overview_drafts
    fetched = []
    pages = {
        "https://lite.duckduckgo.com/lite/": "search",
        "https://www.cdc.gov/skin-cancer/sun-safety/index.html": "CDC guidance explains skin cancer prevention and sun safety for outdoor activity.",
    }
    monkeypatch.setattr(external_effects, "assert_allowed", lambda _kind: None)
    def fake_fetch(url, params=None):
        if not params:
            fetched.append(url)
        return pages[url]
    monkeypatch.setattr(knowledge, "fetch", fake_fetch)
    monkeypatch.setattr(knowledge, "plain", lambda value: value)
    monkeypatch.setattr(knowledge, "check_url", lambda url: url)
    monkeypatch.setattr(knowledge, "search_results", lambda *_args: [])

    result = overview_drafts._research_sources(
        "女童防晒服", "US", [], ["https://www.cdc.gov/skin-cancer/sun-safety/index.html"])

    assert fetched == ["https://www.cdc.gov/skin-cancer/sun-safety/index.html"]
    assert len(result) == 1
    assert result[0]["explicit_reference"] is True
    assert "sun safety" in result[0]["excerpt"]


def test_web_failure_continues_with_inference_instead_of_silently_leaving_modules_blank(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    from backend import model_settings
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    archive_id = "a" * 32
    archive_service.ARCHIVES.mkdir(parents=True)
    archive_service._save({"id": archive_id, "dataset_id": "dataset-unused", "marketplace": "US",
                           "overview": {}, "overview_sources": {}, "integrations": {}})
    monkeypatch.setattr(overview_drafts, "_selected_sources", lambda *_args: ([{"id": "artifact:text", "selected": True}], [
        {"source_id": "artifact:text", "type": "keywords", "content": "plain text source material"}], []))
    monkeypatch.setattr(overview_drafts, "_research_sources", lambda *args, **kwargs: [])
    model_called = []
    monkeypatch.setattr(model_settings, "defaults", lambda: ("fake/text", "fake/vision"))
    monkeypatch.setattr("backend.runtime_isolation.credential_selection", lambda _model: ("fake-provider", None))
    monkeypatch.setattr("backend.batch.provider_config", lambda *_args: ("https://model.example/chat", "fake-key", "fake-model"))
    monkeypatch.setattr("backend.external_effects.assert_allowed", lambda _kind: None)
    monkeypatch.setattr("backend.usage.start_usage_event", lambda **_kwargs: "event")
    monkeypatch.setattr("backend.usage.finish_usage_event", lambda *_args, **_kwargs: None)
    class Response:
        def raise_for_status(self): pass
        def json(self):
            model_called.append(True)
            return {"choices": [{"message": {"content": json.dumps({
                "segments": [{"id": "S1", "name": "暂定细分", "description": "需验证"}],
                "product_forms": [{"id": "P1", "name": "暂定形态", "description": "需验证",
                                   "distinguishing_evidence": "当前无公开资料"}],
                "modules": {field: [{"segment_id": "S1", "text": "该品类的分析仍需结合具体资料验证。",
                                     "basis": "inference", "basis_note": "当前没有可用本地或网页证据，属于暂定推断。"}]
                            for field in overview_drafts.OVERVIEW_FIELDS},
            })}}], "usage": {"total_tokens": 10}}
    monkeypatch.setattr("httpx.post", lambda *_args, **_kwargs: Response())
    run_id = "b" * 32
    overview_drafts._save(archive_id, run_id, {"id": run_id, "archive_id": archive_id,
        "status": "running", "local_only": False, "category_context": "女童防晒服",
        "data_month_context": "2026-06", "selected_source_ids": ["artifact:text"],
        "suggestions": {}, "error": None})
    overview_drafts._worker(archive_id, run_id)
    result = overview_drafts.get(archive_id, run_id)
    assert result["status"] == "ready"
    assert result["public_research_status"] == "unavailable"
    assert model_called
    assert result["suggestions"]["product_function"][0]["basis"] == "inference"


def test_overview_draft_reports_pre_model_failure_without_claiming_analysis_ran(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    archive_id = "d" * 32
    archive_service.ARCHIVES.mkdir(parents=True)
    archive_service._save({"id": archive_id, "dataset_id": "dataset-unused", "marketplace": "US",
                           "overview": {}, "overview_sources": {}, "integrations": {}})

    def fail_source_read(*_args):
        raise AttributeError("bad source shape")

    monkeypatch.setattr(overview_drafts, "_selected_sources", fail_source_read)
    run_id = "e" * 32
    overview_drafts._save(archive_id, run_id, {"id": run_id, "archive_id": archive_id,
        "status": "running", "local_only": True, "category_context": "测试品类",
        "data_month_context": "2026-05", "selected_source_ids": [], "suggestions": {}, "error": None})

    overview_drafts._worker(archive_id, run_id)
    result = overview_drafts.get(archive_id, run_id)

    assert result["status"] == "failed"
    assert result.get("model_attempted") is not True
    assert "尚未调用模型" in result["error"]
    assert result["diagnostics"]["stage"] == "source_preparation"


def test_ready_draft_is_visible_and_can_be_accepted(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        run_id = "c" * 32
        item = {"id": "draft-suggestion", "text": "Sun protection clothing", "source_type": "overview_draft",
                "source_ids": [run_id], "scope": {"category_context": "女童防晒服"},
                "links": [{"url": "https://example.org/source", "excerpt": "Source excerpt"}],
                "updated_at": "2026-10-18T00:00:00Z"}
        overview_drafts._finish(archive["id"], run_id, {"id": run_id, "archive_id": archive["id"],
            "status": "ready", "suggestions": {"product_function": [item]}, "error": None,
            "usage": {"total_tokens": 12}})
        current = archive_service.get(archive["id"])
        suggestion = current["overview_suggestions"]["product_function"][0]
        assert suggestion["links"][0]["excerpt"] == "Source excerpt"
        response = client.patch(f"/api/product-archive/archives/{archive['id']}", json={
            "accepted_suggestion_ids": [suggestion["id"]], "expected_updated_at": current["updated_at"],
        })
        assert response.status_code == 200, response.text
        assert response.json()["overview"]["product_function"] == item["text"]
        assert response.json()["overview_sources"]["product_function"]["source_type"] == "overview_draft"


def test_draft_model_sample_contains_aggregates_not_identifiers(monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "_source", lambda _dataset_id: ({}, [
        {"raw": {"ASIN": "B000000001", "Brand": "Brand A", "Name": "Product title", "Price": 10}},
        {"raw": {"ASIN": "B000000002", "Brand": "Brand B", "Name": "Second title", "Price": 20}},
    ]))
    result = overview_drafts._compact_sample({"dataset_id": "d", "marketplace": "US", "mapping": {
        "asin": "ASIN", "brand": "Brand", "product_name": "Name", "price": "Price"}})
    serialized = json.dumps(result)
    assert "B000000001" not in serialized
    assert "Brand A" not in serialized
    assert "Product title" not in serialized
    assert result["category_level_distributions"]["price"]["top_values"] == [
        {"value": "10", "rows": 1}, {"value": "20", "rows": 1}]


def test_draft_model_receives_keywords_only_from_completed_keyword_runs(monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "_source", lambda _dataset_id: ({}, []))
    archive = {"dataset_id": "dataset", "integrations": {"keyword_research": {
        "sources": [{"run_id": "completed-run", "keyword_count": 1}],
        "representative_terms": [
            {"keyword": "task result", "source_run_id": "completed-run"},
            {"keyword": "orphan suggestion", "source_run_id": "unfinished-run"},
            {"keyword": "missing source id"},
        ],
    }}}

    result = overview_drafts._compact_sample(archive)

    assert result["keyword_research_terms"] == ["task result"]


def test_public_research_falls_back_to_archive_keywords_after_empty_js_page(monkeypatch):
    from backend import knowledge
    from backend.product_archive import overview_drafts
    from backend import external_effects
    queries = []
    fetches = []
    pages = {
        "https://lite.duckduckgo.com/lite/": "search",
        "https://shop.example/js-page": "",
        "https://brand.example/sun-protection": "Girls sun protection clothing for swimming and outdoor play.",
    }
    monkeypatch.setattr(external_effects, "assert_allowed", lambda _kind: None)
    def fake_fetch(url, params=None):
        if params:
            queries.append(params["q"])
        else:
            fetches.append(url)
        return pages[url]
    monkeypatch.setattr(knowledge, "fetch", fake_fetch)
    monkeypatch.setattr(knowledge, "plain", lambda value: value)
    monkeypatch.setattr(knowledge, "check_url", lambda url: url)
    def fake_search(_html, query, *_args):
        return ([{"url": "https://shop.example/js-page", "title": "Taobao", "publisher": "shop.example"}]
                if "use cases" in query else
                [{"url": "https://brand.example/sun-protection", "title": "Sun protection clothing", "publisher": "brand.example"}])
    monkeypatch.setattr(knowledge, "search_results", fake_search)

    sources = overview_drafts._research_sources(
        "女童防晒服", "US", ["girls sun protection clothing", "swim cover up"])

    assert len(queries) == 4
    assert "girls sun protection clothing" in queries[0]
    assert "swim cover up" in queries[1]
    assert "swim cover up" in queries[2]
    assert "girls sun protection clothing" in queries[3]
    assert "use cases" in queries[1]
    assert len(fetches) == 2
    assert sources[0]["url"] == "https://brand.example/sun-protection"
    assert "Girls sun protection clothing" in sources[0]["excerpt"]


def test_draft_suggestions_require_valid_field_specific_evidence():
    from backend.product_archive import overview_drafts
    sources = [{"evidence_id": "web:source-a", "url": "https://example.org/a", "title": "A",
                "excerpt": "Evidence A"},
               {"evidence_id": "web:source-b", "url": "https://example.org/b", "title": "B",
                "excerpt": "Evidence B"}]
    evidence = {"local_sample:aggregate": {"source_type": "local_sample", "source_id": "sample"},
                "keyword:term-a": {"source_type": "keyword_research", "source_id": "term-a"},
                **{item["evidence_id"]: {"source_type": "web", "source_id": item["evidence_id"]}
                   for item in sources}}
    generated = {
        "product_function": {"text": "Keeps items dry", "evidence_refs": ["web:source-a"]},
        "target_audience": {"text": "Families", "evidence_refs": ["not-real"]},
        "seasonality": {"text": "Summer", "evidence_refs": ["local_sample:aggregate"]},
        "representative_keywords": {"text": "sun protection", "evidence_refs": ["keyword:term-a"]},
        "usage_scenarios": {"text": "泳池课程", "evidence_refs": ["web:source-b"]},
    }

    result = overview_drafts._build_suggestions("run-1", generated, evidence, sources, "Category", "2026-06",
                                                comparable_months=["2026-06"])

    assert list(result) == ["product_function", "usage_scenarios"]
    assert result["product_function"][0]["links"] == [sources[0]]
    assert result["product_function"][0]["source_ids"] == ["web:source-a"]
    assert result["usage_scenarios"][0]["links"] == [sources[1]]


def test_overview_draft_accepts_segmented_module_entries_and_buying_motives():
    from backend.product_archive import overview_drafts
    evidence = {"local_sample:aggregate": {"source_type": "local_sample"}}
    generated = {
        "segments": [{"id": "S1", "name": "周末亲子户外家庭"}],
        "modules": {
            "product_function": [{"segment_id": "S1", "text": "为儿童户外活动提供遮阳与活动便利。", "basis": "synthesis", "evidence_refs": ["local_sample:aggregate"]}],
            "buying_motives": [{"segment_id": "S1", "text": "家长希望减少日晒顾虑并让孩子愿意持续活动。", "basis": "inference", "evidence_refs": ["local_sample:aggregate"]}],
        },
        "needs_validation": [{"segment_id": "S1", "text": "具体防晒效果待测试"}],
    }

    result, diagnostics = overview_drafts._build_analysis("run-1", generated, evidence, [], "女童防晒服", "2026-05", [])

    assert result["product_function"][0]["segment_id"] == "S1"
    assert result["buying_motives"][0]["basis"] == "inference"
    assert diagnostics["filtered"] == []
    assert diagnostics["needs_validation"][0]["text"] == "具体防晒效果待测试"


def test_overview_source_inventory_only_lists_linked_finished_outputs(monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "_read", lambda _archive_id: {
        "id": "a" * 32, "dataset_id": "dataset-a", "workspace_id": "workspace-a",
        "integrations": {"keyword_research": {"sources": [{"run_id": "keyword-a", "keyword_count": 2}]}},
    })
    monkeypatch.setattr(archive_service, "list_archives", lambda: [{
        "id": "a" * 32, "dataset": {"workspace_id": "workspace-a"}}])
    monkeypatch.setattr(overview_drafts, "_archive_images", lambda _dataset_id: [{"path": "one.jpg"}])
    from backend import project_files
    artifacts = [
        {"id": "tag", "kind": "data_x", "source_type": "data_x_snapshot", "name": "Data-X-V4.xlsx",
         "downloadable": True, "metadata": {"dataset_id": "dataset-a", "version": 4}},
        {"id": "keyword", "kind": "keywords", "source_type": "keyword_research", "source_id": "keyword-a",
         "name": "result.xlsx", "downloadable": True, "metadata": {}},
        {"id": "review", "kind": "reviews", "source_type": "amazon_reviews", "source_id": "review-a:result.xlsx",
         "name": "Amazon-Reviews.xlsx", "downloadable": True,
         "metadata": {"job_id": "review-a", "source_archive_id": "a" * 32, "source_dataset_id": "dataset-a"}},
        {"id": "voc", "kind": "voc", "source_type": "amazon_voc_output", "name": "VOC-report.html",
         "downloadable": True, "metadata": {"run_id": "voc-a"}},
        {"id": "other-image", "kind": "images", "source_type": "dataset_asset", "name": "image.jpg",
         "downloadable": True, "metadata": {}},
        {"id": "other-plan", "kind": "keywords", "source_type": "keyword_run_plan", "name": "plan.json",
         "downloadable": True, "metadata": {}},
        {"id": "other-review", "kind": "reviews", "source_type": "amazon_reviews",
         "source_id": "other:result.xlsx", "name": "Amazon-Reviews.xlsx", "downloadable": True,
         "metadata": {"job_id": "other", "source_archive_id": "other", "source_dataset_id": "dataset-a"}},
    ]
    monkeypatch.setattr(project_files, "list_artifacts", lambda workspace_id: artifacts if workspace_id == "workspace-a" else [])
    monkeypatch.setattr("backend.keyword_research.registry.read", lambda _run_id: {
        "status": "done", "source_archive_id": "a" * 32, "source_dataset_id": "dataset-a"})
    monkeypatch.setattr("backend.amazon_reviews.service.get", lambda _job_id: {
        "can_download": True, "status": "completed_with_errors",
        "source_archive_id": "a" * 32, "source_dataset_id": "dataset-a"})
    monkeypatch.setattr("backend.amazon_voc.service.service.get_run", lambda _run_id: {
        "status": "completed", "workspace_id": "workspace-a", "sources": {}})

    sources = overview_drafts.list_sources("a" * 32)

    assert {source["id"] for source in sources} == {"artifact:tag", "artifact:keyword", "artifact:review", "artifact:voc"}
    assert {source["type"] for source in sources} == {"tagged_workbook", "keywords", "reviews", "voc"}
    assert "1 张关联图片" in sources[0]["coverage"]
    assert all(source["selected_by_default"] for source in sources if source["type"] != "reviews")
    assert not next(source for source in sources if source["type"] == "reviews")["selected_by_default"]
    assert next(source for source in sources if source["type"] == "reviews")["status"] == "completed_with_errors"
    assert all("path" not in source for source in sources)
    monkeypatch.setattr(overview_drafts, "_read_artifact_text", lambda *_args: {"sheets": []})
    monkeypatch.setattr(overview_drafts, "_completed_tag_features", lambda _dataset_id: [{"feature": "style"}])
    monkeypatch.setattr(overview_drafts, "_vision_model_ready", lambda: True)
    archive = archive_service._read("a" * 32)
    _, selected, issues = overview_drafts._selected_sources(archive, ["artifact:tag", "artifact:keyword"])
    assert not issues
    assert selected[0]["type"] == "tagged_workbook"
    assert selected[0]["content"]["completed_tag_features"] == [{"feature": "style"}]
    assert selected[0]["image_paths"] == ["one.jpg"]
    assert selected[1]["task_run_id"] == "keyword-a"
    with pytest.raises(ValueError, match="不属于当前档案"):
        overview_drafts._selected_sources(archive, ["artifact:other-image"])


def test_completed_tag_results_are_not_an_extra_checkbox(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        with Session() as session:
            dataset_id = archive["dataset_id"]
            product = session.scalars(select(Product).where(Product.dataset_id == dataset_id).order_by(Product.row)).first()
            schema = Schema(dataset_id=dataset_id, version=1, definitions=[], status="approved")
            session.add(schema)
            session.flush()
            run = Run(dataset_id=dataset_id, schema_id=schema.id, kind="full", status="completed",
                      product_ids=[product.id], errors={})
            session.add(run)
            session.flush()
            session.add(Tag(run_id=run.id, product_id=product.id, values={"户外场景": "泳池"}, review="approved"))
            session.commit()

        sources = client.get(f"/api/product-archive/archives/{archive['id']}/overview-sources").json()
        assert all(item["type"] != "completed_tags" for item in sources)
        tags = __import__("backend.product_archive.overview_drafts", fromlist=["_completed_tag_features"])._completed_tag_features(archive["dataset_id"])
        assert tags[0]["feature"] == "户外场景"
        assert tags[0]["values"] == [{"value": "泳池", "rows": 1}]


def test_overview_structured_output_reports_missing_fields_and_invalid_evidence():
    from backend.product_archive import overview_drafts
    generated = {"modules": {"usage_scenarios": [{"text": "泳池活动", "evidence_refs": ["bad-id"]}]}}

    result, diagnostics = overview_drafts._build_analysis("run-2", generated, {}, [], "品类", "2026-05", [])

    assert result == {}
    assert "product_function" in diagnostics["missing_fields"]
    assert any("无效资料编号" in item["reason"] for item in diagnostics["filtered"])


def test_usage_period_can_be_inferred_but_sales_peak_needs_comparable_months():
    from backend.product_archive import overview_drafts
    evidence = {"local_sample:aggregate": {"source_type": "local_sample"}}
    generated = {"segments": [{"id": "S1", "name": "水上活动"}], "modules": {"seasonality": [
        {"segment_id": "S1", "claim_type": "usage_period", "basis": "inference",
         "basis_note": "基于户外水上活动的通常季节判断，需结合当地气候验证。", "text": "主要使用时段可能集中在当地温暖月份。"},
        {"segment_id": "S1", "claim_type": "sales_peak", "basis": "observation",
         "text": "五月是销售旺季。", "evidence_refs": ["local_sample:aggregate"]},
    ]}}

    result, diagnostics = overview_drafts._build_analysis("run-3", generated, evidence, [], "女童防晒服", "2026-05", ["2026-05"])

    assert len(result["seasonality"]) == 1
    assert result["seasonality"][0]["basis"] == "inference"
    assert any("3个不同月份" in item["reason"] for item in diagnostics["filtered"])


def test_seasonality_needs_three_distinct_comparable_demand_months():
    from backend.product_archive import overview_drafts
    archive = {"data_month": "2026-06", "integrations": {"keyword_research": {
        "sources": [{"data_month": "2026-04", "keyword_count": 10},
                    {"data_month": "2026-05", "keyword_count": 12},
                    {"data_month": "2026-06", "keyword_count": 8}],
        "representative_terms": []}}}
    assert overview_drafts._comparable_demand_months(archive) == ["2026-04", "2026-05", "2026-06"]
    assert overview_drafts._seasonality_validation(["2026-06"]) == {
        "status": "pending_validation", "reason": "至少需要3个不同月份的可比销量或搜索需求证据",
        "observed_months": ["2026-06"]}
    assert overview_drafts._seasonality_validation(["2026-04", "2026-05", "2026-06"]) is None


def test_draft_response_exposes_pending_seasonality_validation(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    archive_id, run_id = "d" * 32, "e" * 32
    validation = {"seasonality": {"status": "pending_validation",
        "reason": "至少需要3个不同月份的可比销量或搜索需求证据", "observed_months": ["2026-06"]}}
    overview_drafts._save(archive_id, run_id, {"id": run_id, "archive_id": archive_id, "status": "ready",
        "suggestions": {}, "needs_validation": validation, "error": None, "usage": None})

    response = overview_drafts.get(archive_id, run_id)

    assert response["needs_validation"] == validation


def _run_fake_overview_worker(tmp_path, monkeypatch, response_payload, *, research=None,
                              record_usage=False, materials_override=None):
    """Run the worker against fake files, web research, model, and optional usage ledger."""
    import json as json_module
    from backend import model_settings
    from backend.product_archive import overview_drafts

    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    archive_service.ARCHIVES.mkdir(parents=True, exist_ok=True)
    archive_id, run_id = "f" * 32, "a" * 32
    archive_service._save({
        "id": archive_id, "dataset_id": "dataset-unused", "marketplace": "US",
        "category": "Synthetic category", "overview": {}, "overview_sources": {},
        "integrations": {},
    })
    materials = materials_override or [{"source_id": "artifact:local", "type": "reviews", "content": "synthetic local evidence"}]
    monkeypatch.setattr(overview_drafts, "_selected_sources", lambda *_args: ([
        {"id": "artifact:local", "type": "reviews", "selected": True},
    ], materials, []))
    research_calls = []

    def fake_research(*args, **kwargs):
        research_calls.append((args, kwargs))
        if isinstance(research, Exception):
            raise research
        if callable(research):
            return research(*args, **kwargs)
        excluded = {str(url).rstrip("/") for url in (kwargs.get("excluded_urls") or set())}
        return [item for item in (research or []) if str(item.get("url") or "").rstrip("/") not in excluded]

    monkeypatch.setattr(overview_drafts, "_research_sources", fake_research)
    monkeypatch.setattr(model_settings, "defaults", lambda: ("fake/text", "fake/vision"))
    monkeypatch.setattr("backend.runtime_isolation.credential_selection",
                        lambda _model: ("fake-provider", "fake-credential-id"))
    monkeypatch.setattr("backend.batch.provider_config",
                        lambda *_args: ("https://model.example/chat", "synthetic-key", "fake-model"))
    monkeypatch.setattr("backend.external_effects.assert_allowed", lambda _service=None: None)

    if not record_usage:
        monkeypatch.setattr("backend.usage.start_usage_event", lambda **_kwargs: "fake-event")
        monkeypatch.setattr("backend.usage.finish_usage_event", lambda *_args, **_kwargs: None)

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    requests = []

    def fake_post(endpoint, *, headers, json, timeout):
        requests.append({"endpoint": endpoint, "headers": headers, "body": json, "timeout": timeout})
        prompt = json_module.loads(json["messages"][0]["content"])
        instruction = prompt.get("instruction", "")
        if "研究规划员" in instruction:
            payload = {"choices": [{"message": {"content": '{"questions":["产品形态与细分"],"queries":["synthetic category product forms"]}'},
                                    "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
        elif "研究覆盖检查员" in instruction:
            payload = {"choices": [{"message": {"content": '{"questions":["尚待核验的差异"],"queries":["synthetic category needs and seasonality"]}'},
                                    "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
        else:
            try:
                final_payload = json_module.loads(response_payload["choices"][0]["message"]["content"])
                if "segments" not in final_payload and isinstance(final_payload.get("modules"), dict):
                    from backend.product_archive import overview_drafts
                    final_payload["segments"] = [{"id": "S1", "name": "Synthetic segment", "description": "synthetic"}]
                    final_payload["product_forms"] = [{"id": "P1", "name": "Synthetic form", "description": "synthetic",
                                                        "distinguishing_evidence": "synthetic"}]
                    final_payload["modules"] = {field: final_payload["modules"].get(field) or [{
                        "segment_id": "S1", "text": "Synthetic complete analysis for validation.",
                        "basis": "inference", "basis_note": "Synthetic test evidence is intentionally limited."}]
                        for field in overview_drafts.OVERVIEW_FIELDS}
                    payload = response_payload | {"choices": [response_payload["choices"][0] | {"message":
                        response_payload["choices"][0]["message"] | {"content": json_module.dumps(final_payload)}}]}
                else:
                    payload = response_payload
            except (ValueError, KeyError, IndexError, TypeError):
                payload = response_payload
        return FakeResponse(payload)

    monkeypatch.setattr("httpx.post", fake_post)
    overview_drafts._save(archive_id, run_id, {
        "id": run_id, "archive_id": archive_id, "status": "running",
        "local_only": False, "category_context": "Synthetic category",
        "data_month_context": "2026-05", "selected_source_ids": ["artifact:local"],
        "suggestions": {}, "error": None,
    })
    overview_drafts._worker(archive_id, run_id)
    return overview_drafts.get(archive_id, run_id), requests, research_calls, run_id


def test_overview_worker_researches_in_two_rounds_and_includes_web_evidence_in_final_prompt(tmp_path, monkeypatch):
    web_source = {
        "evidence_id": "web:public-source", "title": "Public guidance",
        "url": "https://www.example.test/guidance", "excerpt": "Synthetic public evidence excerpt",
        "publisher": "example.test", "retrieved_at": "2026-10-19T00:00:00Z",
    }
    response_payload = {
        "choices": [{"message": {"content": json.dumps({"modules": {
            "product_function": [{"text": "Synthetic function analysis", "basis": "synthesis",
                                  "evidence_refs": ["web:public-source"]}],
        }})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    result, requests, research_calls, _run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload, research=[web_source])

    assert len(research_calls) == 2
    assert len(requests) == 3
    prompt = json.loads(requests[-1]["body"]["messages"][0]["content"])
    assert prompt["sources"] == [web_source]
    assert {item["id"] for item in prompt["evidence_refs"]} >= {"web:public-source"}
    assert result["status"] == "ready"
    assert result["suggestions"]["product_function"][0]["source_ids"] == ["web:public-source"]


def test_worker_sends_complete_prompt_within_text_character_budget(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    response_payload = {"choices": [{"message": {"content": json.dumps({"modules": {
        "product_function": [{"text": "Synthetic function analysis", "basis": "synthesis",
                              "evidence_refs": ["artifact:local"]}],
    }})}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 10}}
    large_material = [{"source_id": "artifact:local", "type": "reviews", "content": "x" * 65000}]

    result, requests, _research_calls, _run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload, materials_override=large_material)

    request_text = requests[-1]["body"]["messages"][0]["content"]
    assert len(request_text) <= overview_drafts.MAX_ANALYSIS_TEXT_CHARS
    assert result["input_routing"]["text_chars"] == len(request_text)
    assert result["input_routing"]["text_chars_max"] == overview_drafts.MAX_ANALYSIS_TEXT_CHARS
    assert result["input_routing"]["material_truncated"] is True
    assert result["status"] == "ready"


def test_worker_keeps_output_filter_reasons_when_every_module_entry_is_rejected(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    response_payload = {"choices": [{"message": {"content": json.dumps({
        "segments": [{"id": "S1", "name": "Valid segment"}],
        "modules": {"product_function": [{"segment_id": "S99", "text": "Invalid segment",
                                            "basis": "invalid", "evidence_refs": ["artifact:local"]}]},
    })}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 10}}

    result, requests, _research_calls, _run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload)

    assert len(requests) == 2
    assert result["status"] == "failed"
    assert result["diagnostics"]["filtered"]
    assert "未通过校验" in result["diagnostics"]["message"]
    assert {item["reason"] for item in result["diagnostics"]["filtered"]} == {"结论依据类型无效"}


def test_overview_research_has_hard_search_page_budget_and_deduplicates_urls(monkeypatch):
    from backend import external_effects, knowledge
    from backend.product_archive import overview_drafts

    search_queries, fetched_pages = [], []
    duplicate = "https://www.example.test/a"

    def fake_fetch(url, params=None):
        if params:
            search_queries.append(params["q"])
            return "synthetic search response"
        fetched_pages.append(url)
        return "synthetic category evidence"

    monkeypatch.setattr(external_effects, "assert_allowed", lambda _service=None: None)
    monkeypatch.setattr(knowledge, "fetch", fake_fetch)
    monkeypatch.setattr(knowledge, "plain", lambda value: value)
    monkeypatch.setattr(knowledge, "check_url", lambda url: url)
    monkeypatch.setattr(knowledge, "search_results", lambda *_args: [
        {"title": "A", "url": duplicate, "publisher": "example.test"},
        {"title": "A duplicate", "url": duplicate, "publisher": "example.test"},
        {"title": "B", "url": "https://www.example.test/b", "publisher": "example.test"},
    ])

    result = overview_drafts._research_sources(
        "synthetic category", "US", [], [duplicate, "https://www.example.test/c", "https://www.example.test/c"])

    assert len(search_queries) <= overview_drafts.MAX_PUBLIC_SEARCHES
    assert len(fetched_pages) <= 3
    assert len(fetched_pages) == len(set(fetched_pages))
    assert len({item["url"] for item in result}) == len(result)


def test_overview_web_failure_is_explicit_and_inference_is_labeled(tmp_path, monkeypatch):
    response_payload = {
        "choices": [{"message": {"content": json.dumps({"modules": {
            "product_function": [{"text": "Tentative category inference", "basis": "inference",
                                  "basis_note": "No public source was available; verify with category evidence."}],
        }})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    result, requests, research_calls, _run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload, research=RuntimeError("synthetic network failure"))

    assert len(research_calls) == 2
    assert len(requests) == 2
    assert result["public_research_status"] == "unavailable"
    suggestion = result["suggestions"]["product_function"][0]
    assert suggestion["basis"] == "inference"
    assert suggestion["source_type"] == "model_inference"
    assert suggestion["basis_note"]


def test_public_research_distinguishes_empty_results_from_search_challenges(monkeypatch):
    from backend.product_archive import overview_drafts
    from backend import external_effects, knowledge

    monkeypatch.setattr(external_effects, "assert_allowed", lambda _service=None: None)
    monkeypatch.setattr(knowledge, "fetch", lambda *_args, **_kwargs: "<html>anomaly-modal captcha</html>")
    monkeypatch.setattr(knowledge, "search_results", lambda *_args: [])
    challenged = {}
    assert overview_drafts._research_sources("rash guard", "US", diagnostics=challenged) == []
    assert challenged["status"] == "unavailable"
    assert challenged["search_attempts"] == overview_drafts.MAX_INITIAL_SEARCHES
    assert challenged["search_failures"] == overview_drafts.MAX_INITIAL_SEARCHES
    assert challenged["failure_types"]["RuntimeError"] == overview_drafts.MAX_INITIAL_SEARCHES

    monkeypatch.setattr(knowledge, "fetch", lambda *_args, **_kwargs: "<html>ordinary empty page</html>")
    empty = {}
    assert overview_drafts._research_sources("rash guard", "US", diagnostics=empty) == []
    assert empty["status"] == "no_results"
    assert empty["search_successes"] == overview_drafts.MAX_INITIAL_SEARCHES
    assert empty["candidate_count"] == 0


def test_public_research_queries_keyword_clusters_separately(monkeypatch):
    from backend.product_archive import overview_drafts
    from backend import external_effects, knowledge

    seen_queries = []
    monkeypatch.setattr(external_effects, "assert_allowed", lambda _service=None: None)
    monkeypatch.setattr(knowledge, "fetch", lambda _url, params=None: seen_queries.append(params["q"]) or "<html/>" )
    monkeypatch.setattr(knowledge, "search_results", lambda *_args: [])
    overview_drafts._research_sources("女童防晒衣", "US",
        ["rash guard for girls", "girls swim shirt", "kids sun hoodie"])

    assert len(seen_queries) == overview_drafts.MAX_INITIAL_SEARCHES
    assert any("rash guard for girls" in query for query in seen_queries)
    assert any("girls swim shirt" in query for query in seen_queries)
    assert any("kids sun hoodie" in query for query in seen_queries)
    assert not any("rash guard for girls girls swim shirt kids sun hoodie" in query for query in seen_queries)


def test_public_research_counts_empty_pages_as_unavailable(monkeypatch):
    from backend.product_archive import overview_drafts
    from backend import external_effects, knowledge

    monkeypatch.setattr(external_effects, "assert_allowed", lambda _service=None: None)
    monkeypatch.setattr(knowledge, "fetch", lambda url, params=None: "search response" if params else "")
    monkeypatch.setattr(knowledge, "search_results", lambda *_args: [
        {"title": "Empty page", "url": "https://example.test/empty", "publisher": "example.test"}])
    monkeypatch.setattr(knowledge, "check_url", lambda url: url)
    monkeypatch.setattr(knowledge, "plain", lambda value: value)
    diagnostics = {}

    result = overview_drafts._research_sources("rash guard", "US", diagnostics=diagnostics)

    assert result == []
    assert diagnostics["candidate_count"] > 0
    assert diagnostics["page_fetch_attempts"] == 1
    assert diagnostics["page_fetch_successes"] == 0
    assert diagnostics["page_fetch_failures"] == 1
    assert diagnostics["status"] == "unavailable"


def test_analysis_prompt_budget_covers_the_entire_serialized_request():
    from backend.product_archive import overview_drafts
    prompt = {
        "instruction": "fixed",
        "sample_summary": {"keyword_evidence": [{"id": f"kw:{i}", "term": "term"} for i in range(20)]},
        "materials": [{"source_id": "workbook", "type": "tagged_workbook", "content": {"rows": ["x" * 18000]}}],
        "sources": [{"evidence_id": "web:1", "excerpt": "e" * 5000}],
        "evidence_refs": [{"id": f"kw:{i}"} for i in range(20)],
    }
    result, truncated, chars = overview_drafts._fit_prompt_to_char_budget(prompt, 12000)
    assert truncated
    assert chars <= 12000
    assert chars == len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def test_analysis_filters_unknown_segment_and_invalid_basis():
    from backend.product_archive import overview_drafts
    result, diagnostics = overview_drafts._build_analysis(
        "run", {
            "segments": [{"id": "S1", "name": "水上活动"}],
            "modules": {"product_function": [
                {"segment_id": "S2", "text": "未知细分内容", "basis": "synthesis",
                 "evidence_refs": ["local_sample:aggregate"]},
                {"segment_id": "S1", "text": "无效依据类型内容", "basis": "guess",
                 "evidence_refs": ["local_sample:aggregate"]},
            ]},
        }, {"local_sample:aggregate": {"source_type": "local_sample"}}, [], "rash guard", "2026-05", [])
    assert result == {}
    assert {item["reason"] for item in diagnostics["filtered"]} == {
        "引用了未声明的细分编号", "结论依据类型无效"}


@pytest.mark.parametrize(("finish_reason", "content"), [
    ("length", '{"modules":{}}'),
    ("stop", "not valid JSON"),
])
def test_overview_worker_rejects_truncated_or_invalid_json_without_ready_empty_result(
        tmp_path, monkeypatch, finish_reason, content):
    response_payload = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    result, requests, _research_calls, _run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload)

    assert len(requests) == 2
    assert result["status"] == "failed"
    assert result["suggestions"] == {}


def test_overview_truncated_response_keeps_reported_usage_without_claiming_success(tmp_path, monkeypatch):
    from backend import usage

    response_payload = {
        "choices": [{"message": {"content": '{"modules":{'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 24000, "completion_tokens": 10000, "total_tokens": 34000},
    }
    result, requests, _research_calls, run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload, record_usage=True)

    assert len(requests) == 2
    assert result["status"] == "failed"
    assert result["model_attempted"] is True
    assert result["usage"]["input_tokens"] == 24000
    assert result["usage"]["output_tokens"] == 10000
    assert result["usage"]["cost_status"] == "unknown"
    event = usage.events({"range": "today", "module": "product_archive", "task_id": run_id})["items"][0]
    assert event["status"] == "completed"
    assert event["input_tokens"] == 24000 and event["output_tokens"] == 10000
    assert "cost" not in event


def test_overview_unknown_provider_usage_stays_unknown_in_usage_events(tmp_path, monkeypatch):
    from backend import usage

    response_payload = {
        "choices": [{"message": {"content": json.dumps({"modules": {
            "product_function": [{"text": "Synthetic function analysis", "basis": "synthesis",
                                  "evidence_refs": ["artifact:local"]}],
        }})}, "finish_reason": "stop"}],
        # A total alone does not establish input/output allocation or money.
        "usage": {"total_tokens": 30},
    }

    result, requests, _research_calls, run_id = _run_fake_overview_worker(
        tmp_path, monkeypatch, response_payload, record_usage=True)

    assert result["status"] == "ready"
    assert len(requests) == 2
    event = usage.events({"range": "today", "module": "product_archive", "task_id": run_id})["items"][0]
    assert event["usage_quality"] == "unknown"
    assert event["input_tokens"] is None and event["output_tokens"] is None
    assert event["estimated_input_tokens"] is None and event["estimated_output_tokens"] is None
    assert "cost" not in event


def test_archive_metrics_fixed_boundaries_cr3_links_and_exact_prices():
    mapping = {role: role for role in (
        "asin", "brand", "monthly_sales", "price", "review_count", "rating", "listing_date",
        "seller_country", "fulfillment",
    )}
    rows = []

    def add(index, **values):
        row = {role: None for role in mapping}
        row.update({"asin": f"A{index:02}", "brand": f"B{index:02}", "monthly_sales": 1,
                    "seller_country": "US", "fulfillment": "FBA"})
        row.update(values)
        rows.append(row)

    for index, value in enumerate((0, 100, 101, 500, 501, 1000, 1001, 5000, 5001), 1):
        add(index, review_count=value)
    for index, value in enumerate((0, 3.699, 3.7, 3.999, 4.0, 4.299, 4.3, 4.699, 4.7, 5), 20):
        add(index, rating=value)
    for index, value in enumerate((
        0, 4.99, 5, 9.99, 10, 14.99, 15, 19.99, 20, 24.99, 25, 29.99,
        30, 34.99, 35, 39.99, 40, 49.99, 50, 59.99, 60, 69.99, 70,
    ), 100):
        add(index, price=value)
    for index, value in enumerate(("2026-10-03", "2026-07-03", "2026-04-03", "2025-10-03", "2024-10-03"), 60):
        add(index, listing_date=value)
    add(65, listing_date="2026-10-04")
    add(70, review_count=None, rating=None, price=None, listing_date=None)

    metrics = compute_metrics(rows, mapping, as_of_date="2026-10-03", currency="USD")
    competition = metrics["competition"]
    reviews = {item["label"]: item for item in competition["review_count"]["buckets"]}
    assert [reviews[label]["link_count"] for label in (
        "0–100", "101–500", "501–1,000", "1,001–5,000", "5,001+"
    )] == [2, 2, 2, 2, 1]
    ratings = {item["label"]: item for item in competition["rating"]["buckets"]}
    assert [ratings[label]["link_count"] for label in (
        "[0, 3.7)", "[3.7, 4.0)", "[4.0, 4.3)", "[4.3, 4.7)", "[4.7, 5]"
    )] == [2, 2, 2, 2, 2]
    prices = {item["label"]: item for item in competition["price"]["buckets"]}
    assert [prices[label]["link_count"] for label in (
        "0–<5", "5–<10", "10–<15", "15–<20", "20–<25", "25–<30",
        "30–<35", "35–<40", "40–<50", "50–<60", "60–<70", "70–<80",
    )] == [2] * 11 + [1]
    assert competition["price"]["currency"] == "USD"
    assert competition["price"]["exact_prices"][0] == {
        "price": 0.0, "currency": "USD", "units": 1.0, "link_count": 1,
    }
    ages = {item["label"]: item for item in competition["listing_age"]["buckets"]}
    assert [ages[label]["link_count"] for label in (
        "[0, 3mo)", "[3, 6mo)", "[6, 12mo)", "[1, 2y)", "[2y+]"
    )] == [1, 1, 1, 1, 1]
    assert ages["未识别"]["link_count"] == 1
    for key in ("seller_country", "fulfillment", "review_count", "rating", "price", "listing_age"):
        dist = competition[key]
        assert "link_count" in dist["missing"]
        assert "link_share" in dist["missing"]
        if dist["buckets"]:
            assert all("link_count" in item and "link_share" in item and "unit_share" in item
                       for item in dist["buckets"])
    assert competition["asin"]["unit_concentration"]["cr3"]["share"] == round(3 / len(rows), 6)
    assert competition["brand"]["unit_concentration"]["cr10"]["count"] == 10
    assert competition["asin"]["unit_concentration"]["cr5"]["count"] == 5


def test_archive_tag_filter_recalculates_metrics_using_latest_data_x_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    with TestClient(app) as client:
        archive = _upload(client)
        confirmed = client.put(f"/api/product-archive/archives/{archive['id']}/mapping",
                               json={"mapping": archive["mapping"]})
        assert confirmed.status_code == 200, confirmed.text
        with Session() as session:
            products = session.scalars(select(Product).where(
                Product.dataset_id == archive["dataset_id"]).order_by(Product.row)).all()
            product_ids = [product.id for product in products]
        data = {
            "columns": [{"id": "hard-distinction", "definition": {"name": "产品硬区分"}}],
            "rows": [
                {"id": product_ids[0], "cells": {"hard-distinction": {"status": "ready", "value": "mechanical"}}},
                {"id": product_ids[1], "cells": {"hard-distinction": {"status": "review", "value": "old", "manual": "plush"}}},
                {"id": product_ids[2], "cells": {"hard-distinction": {"status": "ready", "value": "plush"}}},
                {"id": product_ids[3], "cells": {"hard-distinction": {"status": "missing", "value": None}}},
                {"id": product_ids[4], "cells": {"hard-distinction": {"status": "ready", "value": "plush"}}},
            ],
        }
        with Session.begin() as session:
            session.add(Snapshot(dataset_id=archive["dataset_id"], version=1, data=data))

        base = client.get(f"/api/product-archive/archives/{archive['id']}").json()
        assert base["data_x_version"]["version"] == 1
        slicer = next(item for item in base["tag_slicers"] if item["id"] == "hard-distinction")
        values = {item["label"]: item["value"] for item in slicer["values"]}
        assert set(values) == {"mechanical", "plush"}
        filtered = client.post(f"/api/product-archive/archives/{archive['id']}/filter",
                               json={"filters": {"hard-distinction": values["mechanical"]}})
        assert filtered.status_code == 200, filtered.text
        sliced = filtered.json()
        assert sliced["current_filter"] == {"hard-distinction": values["mechanical"]}
        assert sliced["metrics"]["market_size"]["sample_monthly_sales"] == 100
        assert sliced["metrics"]["filtered_analysis_rows"] == 1
        stale = client.post(f"/api/product-archive/archives/{archive['id']}/filter",
                            json={"filters": {"hard-distinction": '"not-a-saved-value"'}})
        assert stale.status_code == 400


def test_top_asin_and_brand_images_follow_sample_sales_order(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ASIN", "品牌", "月销量"])
    sheet.append(["B000000001", "Acme", 10])
    sheet.append(["B000000002", "Acme", 30])
    sheet.append(["B000000003", "Acme", 20])
    stream = io.BytesIO()
    book.save(stream)
    with TestClient(app) as client:
        response = client.post("/api/product-archive/import", data={"marketplace": "US", "category": "Image test"},
                              files={"file": ("images.xlsx", stream.getvalue(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert response.status_code == 200, response.text
        archive = response.json()
        with Session() as session:
            products = session.scalars(select(Product).where(
                Product.dataset_id == archive["dataset_id"]).order_by(Product.row)).all()
            product_ids = [product.id for product in products]
        paths = []
        for index in range(3):
            path = tmp_path / f"image-{index}.png"
            path.write_bytes(b"local test image " + str(index).encode())
            paths.append(path)
        with Session.begin() as session:
            for product_id, path in zip(product_ids, paths):
                session.add(Asset(product_id=product_id, source="upload", path=str(path),
                                  sha256=f"test-{path.stem}", image_type="main"))
        confirmed = client.put(f"/api/product-archive/archives/{archive['id']}/mapping",
                               json={"mapping": archive["mapping"]})
        assert confirmed.status_code == 200, confirmed.text
        metrics = confirmed.json()["metrics"]["competition"]
        assert [item["name"] for item in metrics["asin"]["top_by_units"][:3]] == [
            "B000000002", "B000000003", "B000000001"]
        brand = metrics["brand"]["top_by_units"][0]
        assert [item["asin"] for item in brand["images"]] == [
            "B000000002", "B000000003", "B000000001"]
        assert all(item["image_url"].startswith("/api/assets/") for item in brand["images"])
