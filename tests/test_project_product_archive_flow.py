import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.product_archive import service as archive_service


def workbook_bytes():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["ASIN", "商品详情链接", "月销量"])
    sheet.append(["B0PARENT01", "https://www.amazon.com/dp/B0CHILD001", 120])
    sheet.append(["B0PARENT02", "https://www.amazon.com/gp/product/B0CHILD002", 80])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_project_import_registers_source_and_archive_and_dispatches_child_asins(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    raw_workbook = workbook_bytes()
    with TestClient(app) as client:
        project_response = client.post("/api/projects", json={"name": "Isolated Product Test"})
        assert project_response.status_code == 200, project_response.text
        project = project_response.json()
        import_response = client.post("/api/product-archive/import", data={
            "marketplace": "US", "category": "Toy Sample", "workspace_id": project["id"],
        }, files={"file": ("sample.xlsx", raw_workbook,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert import_response.status_code == 200, import_response.text
        archive = import_response.json()
        assert archive["workspace_id"] == project["id"]
        assert archive["dataset"]["workspace_id"] == project["id"]

        files_response = client.get(f"/api/projects/{project['id']}/files")
        assert files_response.status_code == 200, files_response.text
        artifacts = files_response.json()["artifacts"]
        source = next(item for item in artifacts if item["kind"] == "source")
        dossier = next(item for item in artifacts if item["kind"] == "product_archive")
        assert source["sha256"]
        assert dossier["downloadable"] is True
        assert "root" not in project
        dossier_download = client.get(f"/api/projects/{project['id']}/artifacts/{dossier['id']}/download")
        assert dossier_download.status_code == 200
        assert json.loads(dossier_download.content)["dataset_id"] == archive["dataset_id"]
        download = client.get(f"/api/projects/{project['id']}/artifacts/{source['id']}/download")
        assert download.status_code == 200
        assert download.content == raw_workbook

        rows = client.get(f"/api/product-archive/archives/{archive['id']}/samples").json()["rows"]
        with pytest.raises(ValueError, match="确认样本映射"):
            archive_service.dispatch_targets(archive["id"], [rows[0]["id"]], child_only=True)

        mapping = {"asin": "ASIN", "url": "商品详情链接", "monthly_sales": "月销量"}
        confirm = client.put(f"/api/product-archive/archives/{archive['id']}/mapping", json={"mapping": mapping})
        assert confirm.status_code == 200, confirm.text
        targets = archive_service.dispatch_targets(archive["id"], [row["id"] for row in rows], child_only=True)
        assert targets["asins"] == ["B0CHILD001", "B0CHILD002"]
        assert targets["selected_rows"] == 2
        assert targets["unique_asins"] == 2

        keyword_response = client.post(
            f"/api/keyword-research/from-product-archive/{archive['id']}/runs",
            json={"product_ids": [row["id"] for row in rows], "child_only": True, "dispatch": "create"},
        )
        assert keyword_response.status_code == 200, keyword_response.text
        keyword_run = keyword_response.json()["runs"][0]
        assert keyword_response.json()["matrix_enabled"] is False
        assert keyword_run["budget"]["retry_budget"] == 0


def test_tagging_dispatch_archive_is_created_once_per_dataset_and_site(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_service, "ARCHIVES", tmp_path / "archives")
    from backend.importer import import_file

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Tagging Dispatch Project"}).json()
        dataset = import_file(workbook_bytes(), "tagging-dispatch.xlsx", workspace_id=project["id"])
        path = f"/api/product-archive/datasets/{dataset['id']}/ensure-archive"

        first = client.post(path, json={"marketplace": "US"})
        assert first.status_code == 200, first.text
        archive = first.json()
        assert archive["dataset_id"] == dataset["id"]
        assert archive["workspace_id"] == project["id"]
        assert archive["mapping_confirmed"] is False

        repeated = client.post(path, json={"marketplace": "US"})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["id"] == archive["id"]
        assert repeated.json()["mapping_confirmed"] is False

        confirmed = client.put(f"/api/product-archive/archives/{archive['id']}/mapping",
                               json={"mapping": archive["mapping"]})
        assert confirmed.status_code == 200, confirmed.text
        after_confirmation = client.post(path, json={"marketplace": "US"})
        assert after_confirmation.status_code == 200, after_confirmation.text
        assert after_confirmation.json()["id"] == archive["id"]
        assert after_confirmation.json()["mapping_confirmed"] is True

        other_site = client.post(path, json={"marketplace": "UK"})
        assert other_site.status_code == 200, other_site.text
        assert other_site.json()["id"] != archive["id"]
        assert other_site.json()["marketplace"] == "UK"
