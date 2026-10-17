import io

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
