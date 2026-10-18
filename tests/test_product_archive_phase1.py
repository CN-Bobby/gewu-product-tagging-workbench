import io
import json

import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import app
from backend.db import Asset, Product, Session, Snapshot
from backend.product_archive.analytics import compute_metrics
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


def test_overview_draft_endpoints_return_persistent_status(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(overview_drafts, "_launch", lambda _archive_id, _run_id: None)
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


def test_overview_draft_source_urls_are_validated_and_part_of_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.product_archive import overview_drafts
    monkeypatch.setattr(overview_drafts, "_launch", lambda _archive_id, _run_id: None)
    with TestClient(app) as client:
        archive = _upload(client)
        path = f"/api/product-archive/archives/{archive['id']}/overview-drafts"
        base = {"category_context": "女童防晒服", "data_month_context": "2026-06", "local_only": False}
        first = client.post(path, json={**base, "source_urls": ["https://www.cdc.gov/skin-cancer/sun-safety/index.html"]})
        repeated = client.post(path, json={**base, "source_urls": ["https://www.cdc.gov/skin-cancer/sun-safety/index.html"]})
        distinct = client.post(path, json={**base, "source_urls": ["https://www.fda.gov/"]})
        invalid = client.post(path, json={**base, "source_urls": ["http://example.org/page"]})
        too_many = client.post(path, json={**base, "source_urls": [
            "https://a.example/", "https://b.example/", "https://c.example/", "https://d.example/"]})
        assert first.status_code == 200
        assert repeated.json() == first.json()
        assert distinct.json()["id"] != first.json()["id"]
        assert invalid.status_code == 400
        assert too_many.status_code == 400


def test_explicit_source_url_requires_fetched_relevant_page_body(monkeypatch):
    from backend import external_effects, knowledge
    from backend.product_archive import overview_drafts
    fetched = []
    pages = {
        "https://html.duckduckgo.com/html/": "search",
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


def test_web_failure_stops_before_model_request(tmp_path, monkeypatch):
    from backend.product_archive import overview_drafts
    from backend import model_settings
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    archive_id = "a" * 32
    archive_service.ARCHIVES.mkdir(parents=True)
    archive_service._save({"id": archive_id, "dataset_id": "dataset-unused", "marketplace": "US",
                           "overview": {}, "overview_sources": {}, "integrations": {}})
    monkeypatch.setattr(overview_drafts, "_research_sources", lambda *args, **kwargs: [])
    model_called = []
    monkeypatch.setattr(model_settings, "defaults", lambda: model_called.append(True))
    run_id = "b" * 32
    overview_drafts._save(archive_id, run_id, {"id": run_id, "archive_id": archive_id,
        "status": "running", "local_only": False, "category_context": "女童防晒服",
        "data_month_context": "2026-06", "suggestions": {}, "error": None})
    overview_drafts._worker(archive_id, run_id)
    result = overview_drafts.get(archive_id, run_id)
    assert result["status"] == "web_unavailable"
    assert not model_called


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


def test_public_research_falls_back_to_archive_keywords_after_empty_js_page(monkeypatch):
    from backend import knowledge
    from backend.product_archive import overview_drafts
    from backend import external_effects
    queries = []
    fetches = []
    pages = {
        "https://html.duckduckgo.com/html/": "search",
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
    def fake_search(_html, query):
        return ([{"url": "https://shop.example/js-page", "title": "Taobao", "publisher": "shop.example"}]
                if query.startswith("女童") else
                [{"url": "https://brand.example/sun-protection", "title": "Sun protection clothing", "publisher": "brand.example"}])
    monkeypatch.setattr(knowledge, "search_results", fake_search)

    sources = overview_drafts._research_sources(
        "女童防晒服", "US", ["girls sun protection clothing", "swim cover up"])

    assert len(queries) == 2
    assert queries[0] == "女童防晒服 US"
    assert "girls sun protection clothing" in queries[1]
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
    }

    result = overview_drafts._build_suggestions("run-1", generated, evidence, sources, "Category", "2026-06",
                                                comparable_months=["2026-06"])

    assert list(result) == ["product_function", "representative_keywords"]
    assert result["product_function"][0]["links"] == [sources[0]]
    assert result["product_function"][0]["source_ids"] == ["web:source-a"]
    assert result["representative_keywords"][0]["links"] == []


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
