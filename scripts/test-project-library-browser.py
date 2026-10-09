"""Offline browser regression for the fixed TEST project-library fixture.

Connects only to the loopback TEST instance. It never submits a job or calls a provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


def get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8895")
    parser.add_argument("--reports-dir", type=Path, default=Path(r"K:\GeWuTest\Reports"))
    parser.add_argument("--fixture-manifest", type=Path, default=Path(r"K:\GeWuFixtures\project-library-v1\fixture-manifest.json"))
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    if urlparse(base_url).scheme != "http" or urlparse(base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Browser regression is restricted to a loopback TEST URL.")

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    checks: dict[str, bool] = {}
    external_requests: list[str] = []
    screenshots: list[dict[str, str]] = []
    error = None
    status = None
    browser = None
    try:
        status = get_json(f"{base_url}/api/runtime/status")
        health = get_json(f"{base_url}/api/health")
        if status.get("profile") != "test" or status.get("external_effects", {}).get("policy") != "offline":
            raise AssertionError("Target is not an offline TEST runtime.")
        if health.get("storage") != status.get("data", {}).get("path"):
            raise AssertionError("Health and runtime status disagree on the TEST data directory.")
        projects = get_json(f"{base_url}/api/projects")
        swimwear = next((item for item in projects if "泳衣" in item.get("name", "")), None)
        dog = next((item for item in projects if "狗狗发声玩具" in item.get("name", "")), None)
        if not swimwear or not dog:
            raise AssertionError("The fixed swimwear and dog-toy fixture projects were not found.")

        def library(project: dict) -> dict:
            return get_json(f"{base_url}/api/projects/{project['id']}/library")

        swim_lib = library(swimwear)
        dog_lib = library(dog)
        swim_folders = {item["kind"]: item for item in swim_lib["folders"]}
        dog_folders = {item["kind"]: item for item in dog_lib["folders"]}
        checks["fixture_projects_present"] = True
        checks["four_fixed_folders"] = set(swim_folders) == {"source", "data_x", "reviews", "voc"} and set(dog_folders) == {"source", "data_x", "reviews", "voc"}
        checks["dog_data_x_reviews_voc"] = all(dog_folders[kind]["count"] > 0 for kind in ("data_x", "reviews", "voc"))
        checks["swimwear_source_and_reviews"] = swim_folders["source"]["count"] > 0 and swim_folders["reviews"]["count"] > 0
        if not all(checks.values()):
            raise AssertionError("The fixture API does not contain the expected four-folder acceptance samples.")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed in the project Python environment.") from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-background-networking"])
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script("localStorage.setItem('project-sidebar-open', 'false')")
            page = context.new_page()
            page.on("request", lambda request: external_requests.append(request.url)
                    if urlparse(request.url).scheme in {"http", "https"} and
                    urlparse(request.url).hostname not in {"127.0.0.1", "localhost", "::1"} else None)

            # Expanding the global tree must leave the active business page unchanged.
            page.goto(f"{base_url}/#/keyword", wait_until="domcontentloaded")
            page.get_by_role("button", name="展开项目树").click()
            checks["tree_expand_keeps_current_page"] = page.evaluate("location.hash") == "#/keyword"
            if not checks["tree_expand_keeps_current_page"]:
                raise AssertionError("Expanding the project tree changed the active route.")

            # Excel source preview must render workbook rows and embedded product images.
            page.get_by_role("button", name=re.compile(re.escape(swimwear["name"]))).click()
            swimwear_node = page.locator(".ps-project").filter(
                has=page.get_by_role("button", name=re.compile(re.escape(swimwear["name"])))
            )
            swimwear_node.get_by_role("button", name=re.compile("原始上传数据")).click()
            page.locator(".pm-file-select").first.click()
            page.locator(".pp-table tbody tr").first.wait_for(timeout=15000)
            page.wait_for_function("document.querySelectorAll('.pp-cell-image').length > 0", timeout=15000)
            page.wait_for_function("Array.from(document.querySelectorAll('.pp-cell-image')).some(image => image.complete && image.naturalWidth > 0)", timeout=15000)
            checks["swimwear_excel_and_images"] = page.locator(".pp-table tbody tr").count() > 0 and page.locator(".pp-cell-image").count() > 0
            image_path = args.reports_dir / f"browser-project-library-{stamp}-{run_id[:8]}-excel.png"
            page.screenshot(path=str(image_path), full_page=True)
            screenshots.append({"path": str(image_path), "sha256": sha256(image_path)})

            # Back/forward retain the project and file selection route.
            file_hash = page.evaluate("location.hash")
            page.go_back(wait_until="domcontentloaded")
            folder_hash = page.evaluate("location.hash")
            page.go_forward(wait_until="domcontentloaded")
            checks["browser_back_forward"] = folder_hash.endswith("/folders/source") and page.evaluate("location.hash") == file_hash

            # Reviews and Data X remain workbook previews; VOC remains sandboxed HTML.
            page.goto(f"{base_url}/#/projects/{dog['id']}/folders/data_x", wait_until="domcontentloaded")
            page.locator(".pm-file-select").first.click()
            page.locator(".pp-table tbody tr").first.wait_for(timeout=15000)
            checks["dog_data_x_preview"] = page.locator(".pp-table tbody tr").count() > 0

            page.goto(f"{base_url}/#/projects/{swimwear['id']}/folders/reviews", wait_until="domcontentloaded")
            page.locator(".pm-file-select").first.click()
            page.locator(".pp-table tbody tr").first.wait_for(timeout=15000)
            checks["reviews_workbook_preview"] = page.locator(".pp-table tbody tr").count() > 0

            page.goto(f"{base_url}/#/projects/{dog['id']}/folders/voc", wait_until="domcontentloaded")
            page.locator(".pm-file-select").first.click()
            frame = page.locator("iframe.pp-html")
            frame.wait_for(timeout=15000)
            frame_content = frame.content_frame
            frame_content.locator("body").wait_for(timeout=15000)
            checks["voc_html_preview"] = len(frame_content.locator("body").inner_text(timeout=5000).strip()) > 50
            voc_path = args.reports_dir / f"browser-project-library-{stamp}-{run_id[:8]}-voc.png"
            page.screenshot(path=str(voc_path), full_page=True)
            screenshots.append({"path": str(voc_path), "sha256": sha256(voc_path)})

            # At a phone-width viewport the tree starts collapsed and can reopen without navigation.
            context.close()
            narrow = browser.new_context(viewport={"width": 375, "height": 812})
            narrow.add_init_script("localStorage.setItem('project-sidebar-open', 'false')")
            narrow_page = narrow.new_page()
            narrow_page.goto(f"{base_url}/#/projects/{dog['id']}", wait_until="domcontentloaded")
            narrow_page.get_by_role("button", name="展开项目树").click()
            checks["narrow_tree_expand_keeps_page"] = narrow_page.evaluate("location.hash") == f"#/projects/{dog['id']}"
            if not checks["narrow_tree_expand_keeps_page"]:
                raise AssertionError("Narrow-screen tree expansion changed the active project route.")
            narrow_path = args.reports_dir / f"browser-project-library-{stamp}-{run_id[:8]}-narrow.png"
            narrow_page.screenshot(path=str(narrow_path), full_page=True)
            screenshots.append({"path": str(narrow_path), "sha256": sha256(narrow_path)})
            if external_requests:
                raise AssertionError("The browser attempted a request outside loopback.")
            narrow.close()

    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    fixture_version = None
    fixture_hash = None
    if args.fixture_manifest.is_file():
        fixture = json.loads(args.fixture_manifest.read_text(encoding="utf-8-sig"))
        fixture_version = fixture.get("fixture_version")
        fixture_hash = sha256(args.fixture_manifest)
    report = {
        "schema": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if error is None and all(checks.values()) else "failed",
        "profile": status.get("profile") if status else None,
        "build_id": status.get("source", {}).get("build_id") if status else None,
        "commit": status.get("source", {}).get("commit") if status else None,
        "data_path": status.get("data", {}).get("path") if status else None,
        "fixture_version": fixture_version,
        "fixture_manifest_sha256": fixture_hash,
        "external_requests": len(external_requests),
        "external_request_hosts": sorted({urlparse(url).hostname or "" for url in external_requests}),
        "checks": checks,
        "screenshots": screenshots,
        "error": error,
    }
    report_path = args.reports_dir / f"browser-project-library-{stamp}-{run_id[:8]}.json"
    raw = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    report_path.write_bytes(raw)
    print(json.dumps({"status": report["status"], "report": str(report_path), "sha256": hashlib.sha256(raw).hexdigest(),
                      "build_id": report["build_id"], "commit": report["commit"], "external_requests": report["external_requests"],
                      "error": report["error"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
