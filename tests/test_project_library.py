import hashlib
import io
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient
from openpyxl.drawing.image import Image as XLImage
from PIL import Image

from backend.app import app
from backend import project_files
from backend.db import ProjectArtifact, Session
from backend.templates import create_project


def _xlsx(*, with_image=False, formula=False):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "评论数据"
    sheet.append(["Review", "Image", "Calc"])
    sheet.append(["Helpful review", None, "=1+2" if formula else 3])
    sheet.append(["Second review", None, 4])
    if with_image:
        image_bytes = io.BytesIO()
        Image.new("RGB", (640, 400), (30, 120, 200)).save(image_bytes, format="PNG")
        image_bytes.seek(0)
        sheet.add_image(XLImage(image_bytes), "B2")
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _register(workspace_id, tmp_path, *, kind, source_type, name, source_id="item-1",
              data=None, copy_file=True):
    path = tmp_path / (name.replace("/", "_") + ".source")
    path.write_bytes(data if data is not None else _xlsx())
    item = project_files.register_file(workspace_id, kind, path, source_type=source_type,
                                       source_id=source_id, name=name, copy_file=copy_file)
    return item, path


def test_library_fixed_categories_and_deduplicates_backfilled_copy(tmp_path):
    project = create_project("Library categories")
    wid = project["id"]
    external, source = _register(wid, tmp_path, kind="source", source_type="dataset",
                                 name="source.xlsx", source_id="dataset-1", copy_file=False)
    local = project_files.register_file(wid, "source", source, source_type="dataset",
                                        source_id="dataset-1", name="source.xlsx")
    _register(wid, tmp_path, kind="data_x", source_type="data_x_snapshot",
              name="snapshot.xlsx", source_id="snap-1")
    _register(wid, tmp_path, kind="reviews", source_type="amazon_reviews",
              name="Amazon-Reviews.xlsx", source_id="job-1:result.xlsx")
    _register(wid, tmp_path, kind="reviews", source_type="sorftime_mcp_review_export",
              name="Sorftime.xlsx", source_id="export-1")
    _register(wid, tmp_path, kind="reviews", source_type="amazon_reviews",
              name="Amazon-Reviews-raw.json", source_id="job-1:raw.json")
    _register(wid, tmp_path, kind="voc", source_type="amazon_voc_output",
              name="VOC-report.html", source_id="run:html")
    _register(wid, tmp_path, kind="voc", source_type="amazon_voc_output",
              name="VOC-report.xlsx", source_id="run:excel")
    _register(wid, tmp_path, kind="keywords", source_type="keyword_run_output",
              name="not-a-deliverable.xlsx", source_id="run:report")

    result = __import__("backend.project_library", fromlist=["list_library"]).list_library(wid)
    assert [folder["kind"] for folder in result["folders"]] == ["source", "data_x", "reviews", "voc"]
    source_folder, data_x, reviews, voc = result["folders"]
    assert [folder["label"] for folder in result["folders"]] == [
        "原始上传数据", "打标结果", "评论文件", "VOC 报告"]
    assert source_folder["count"] == 1
    assert source_folder["items"][0]["id"] == local["id"]
    assert source_folder["items"][0]["downloadable"] is True
    assert data_x["count"] == 1
    assert reviews["count"] == 2
    assert voc["count"] == 1
    assert all("relative_path" not in item and "source_id" not in item
               for folder in result["folders"] for item in folder["items"])


def test_external_only_entry_is_listed_without_path_and_cannot_preview(tmp_path):
    project = create_project("External library")
    item, source = _register(project["id"], tmp_path, kind="source", source_type="dataset",
                             name="external.xlsx", source_id="dataset-ext", copy_file=False)
    from backend.project_library import list_library
    entry = list_library(project["id"])["folders"][0]["items"][0]
    assert entry["id"] == item["id"]
    assert entry["downloadable"] is False
    assert entry["previewable"] is False
    assert str(source) not in repr(entry)
    with pytest.raises(ValueError):
        from backend.project_library import workbook_page
        workbook_page(project["id"], item["id"])


def test_workbook_preview_paginates_searches_and_does_not_evaluate_formula(tmp_path):
    project = create_project("Workbook preview")
    item, _ = _register(project["id"], tmp_path, kind="reviews", source_type="amazon_reviews",
                        name="Amazon-Reviews.xlsx", source_id="job:result.xlsx",
                        data=_xlsx(formula=True))
    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project['id']}/library/{item['id']}/workbook",
                              params={"offset": 0, "limit": 1})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["sheets"] == ["评论数据"]
        assert page["sheet"] == "评论数据"
        assert page["headers"] == ["Review", "Image", "Calc"]
        assert page["total"] == 2
        assert page["rows"][0]["excel_row"] == 2
        assert page["rows"][0]["values"][2] is None
        assert page["rows"][0]["images"] == []
        search = client.get(f"/api/projects/{project['id']}/library/{item['id']}/workbook",
                            params={"query": "SECOND", "offset": 0})
        assert search.status_code == 200
        assert search.json()["total"] == 1
        assert search.json()["rows"][0]["excel_row"] == 3
        no_match = client.get(f"/api/projects/{project['id']}/library/{item['id']}/workbook",
                              params={"query": "not present"})
        assert no_match.status_code == 200
        assert no_match.json()["total"] == 0


def test_empty_review_workbook_fails_preview_but_keeps_download(tmp_path):
    book = openpyxl.Workbook()
    book.active.title = "全部评论"
    book.active.append(["ASIN", "评论内容"])
    stream = io.BytesIO()
    book.save(stream)
    project = create_project("Empty legacy reviews")
    item, _ = _register(project["id"], tmp_path, kind="reviews",
                        source_type="sorftime_mcp_review_export", name="old-reviews.xlsx",
                        data=stream.getvalue())

    with TestClient(app) as client:
        preview = client.get(f"/api/projects/{project['id']}/library/{item['id']}/workbook")
        assert preview.status_code == 400
        assert "没有可预览的数据行" in preview.json()["detail"]
        download = client.get(f"/api/projects/{project['id']}/artifacts/{item['id']}/download")
        assert download.status_code == 200
        assert download.content == stream.getvalue()


def test_workbook_preview_serves_only_local_embedded_image_thumbnail(tmp_path, monkeypatch):
    from backend import project_library

    project = create_project("Image preview")
    item, _ = _register(project["id"], tmp_path, kind="reviews", source_type="sorftime_mcp_review_export",
                        name="Sorftime.xlsx", data=_xlsx(with_image=True))
    catalog_builds = 0
    build_catalog = project_library._build_image_catalog

    def counted_build(*args, **kwargs):
        nonlocal catalog_builds
        catalog_builds += 1
        return build_catalog(*args, **kwargs)

    monkeypatch.setattr(project_library, "_build_image_catalog", counted_build)
    with TestClient(app) as client:
        page = client.get(f"/api/projects/{project['id']}/library/{item['id']}/workbook").json()
        image_ref = page["rows"][0]["images"][0]
        assert image_ref["column"] == "Image"
        for _ in range(3):
            image = client.get(
                f"/api/projects/{project['id']}/library/{item['id']}/images/{image_ref['image_id']}")
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/png"
            with Image.open(io.BytesIO(image.content)) as thumbnail:
                assert thumbnail.size == (320, 200)
        assert catalog_builds == 1

        project_root = project_files.project_dir(project["id"])
        registered = next(a for a in project_files.list_artifacts(project["id"]) if a["id"] == item["id"])
        workbook_path = project_root / registered["relative_path"]
        workbook_path.write_bytes(b"replacement after catalog was cached")
        replaced = client.get(
            f"/api/projects/{project['id']}/library/{item['id']}/images/{image_ref['image_id']}")
        assert replaced.status_code == 400
        assert "校验失败" in replaced.json()["detail"]
        assert catalog_builds == 1


def test_wrong_project_hash_mismatch_and_corrupt_workbook_are_clear_errors(tmp_path):
    first = create_project("Owner")
    other = create_project("Other")
    item, _ = _register(first["id"], tmp_path, kind="source", source_type="dataset",
                        name="source.xlsx", source_id="dataset-1")
    with TestClient(app) as client:
        wrong_project = client.get(f"/api/projects/{other['id']}/library/{item['id']}/workbook")
        assert wrong_project.status_code == 400
        assert "不属于当前项目" in wrong_project.json()["detail"]

        path = project_files.artifact_path(first["id"], item["id"])
        path.write_bytes(b"tampered")
        mismatch = client.get(f"/api/projects/{first['id']}/library/{item['id']}/workbook")
        assert mismatch.status_code == 400
        assert "校验失败" in mismatch.json()["detail"]

        digest = hashlib.sha256(b"still-not-a-workbook").hexdigest()
        path.write_bytes(b"still-not-a-workbook")
        with Session.begin() as session:
            record = session.get(ProjectArtifact, item["id"])
            record.sha256 = digest
        corrupt = client.get(f"/api/projects/{first['id']}/library/{item['id']}/workbook")
        assert corrupt.status_code == 400
        assert "损坏" in corrupt.json()["detail"]


def test_html_preview_is_plain_text_and_does_not_render_scripts(tmp_path):
    project = create_project("HTML preview")
    html = "<html><script>globalThis.pwned=true</script><body>Report</body></html>"
    item, _ = _register(project["id"], tmp_path, kind="voc", source_type="amazon_voc_output",
                        name="VOC-report.html", source_id="run:html", data=html.encode())
    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project['id']}/library/{item['id']}/html")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == html


def test_existing_hash_named_copy_is_verified_and_never_overwritten(tmp_path):
    project = create_project("Copy integrity")
    item, source = _register(project["id"], tmp_path, kind="source", source_type="dataset",
                             name="source.xlsx", source_id="dataset-1")
    local = project_files.artifact_path(project["id"], item["id"])
    local.write_bytes(b"changed existing copy")
    with pytest.raises(ValueError, match="SHA-256 校验失败"):
        project_files.register_file(project["id"], "source", source, source_type="dataset",
                                    source_id="dataset-1", name="source.xlsx")
    assert local.read_bytes() == b"changed existing copy"
