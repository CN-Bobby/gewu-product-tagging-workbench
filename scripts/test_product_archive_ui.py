"""Read-only packaged Product Archive UI smoke check.

The browser is restricted to the configured local HTTP origin. This script only
opens archive pages and uses navigation, chart, image-preview, and Data X filter
controls; it never uploads files or starts downstream tasks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright


DEFAULT_BASE_URL = "http://127.0.0.1:49726"
DEFAULT_DOG_ARCHIVE_ID = "6aa40a6808664493abde8c8974b5bad9"
TIMEOUT_MS = 8_000


class CheckFailure(Exception):
    """A safe-to-report smoke-check failure with no page or API content."""


def local_json(base_url: str, path: str) -> dict | list:
    """Fetch JSON from the configured local origin only."""
    base = urlparse(base_url)
    url = f"{base.scheme}://{base.netloc}{path}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        raise CheckFailure("local_api_unavailable") from exc


def discover_swimwear_archive(base_url: str) -> str:
    archives = local_json(base_url, "/api/product-archive/archives")
    if not isinstance(archives, list):
        raise CheckFailure("archive_list_invalid")
    for archive in archives:
        if not isinstance(archive, dict):
            continue
        label = " ".join(str(archive.get(key) or "") for key in ("category", "title", "name"))
        folded = label.casefold()
        if "泳衣" in label or "swimwear" in folded or "swimsuit" in folded:
            archive_id = archive.get("id")
            if isinstance(archive_id, str) and archive_id:
                return archive_id
    raise CheckFailure("swimwear_archive_not_found")


def metric_signature(page) -> str:
    """Read metric-region text in memory for before/after comparison only."""
    return json.dumps(
        page.locator(".pa-stat-grid, .pa-concentration-grid, .pa-dist-grid").all_text_contents(),
        ensure_ascii=False,
    )


def check_archive(page, base_url: str, archive_id: str, set_stage) -> dict[str, bool | str]:
    result = {
        "heading_and_concentration": False,
        "samples_navigation_round_trip": False,
        "price_chart": False,
        "image_lightbox": False,
        "label_slicer_round_trip": False,
        "keyword_signals": False,
        "voc_overview_provenance": False,
        "project_artifacts_visible": False,
    }
    set_stage("open_archive")
    page.goto(f"{base_url}/#/product-archive/{archive_id}", wait_until="domcontentloaded")
    set_stage("check_heading_and_concentration")
    heading = page.locator("main.pa-dossier h1")
    heading.wait_for(state="visible", timeout=TIMEOUT_MS)
    for label in ("销量 · Top 3 实体", "销量 · Top 10 实体", "销售额 · Top 3 实体", "销售额 · Top 10 实体"):
        if page.get_by_text(label, exact=True).count() == 0:
            raise CheckFailure("concentration_labels_missing")
    if heading.inner_text().strip():
        result["heading_and_concentration"] = True

    keyword_section = page.locator(".pa-keyword-signals")
    if keyword_section.count() and page.locator(".pa-keyword-signals tbody tr").count() > 0:
        result["keyword_signals"] = True
    else:
        raise CheckFailure("keyword_insights_missing")
    voc_sources = page.locator(".pa-overview-grid .pa-provenance").filter(has_text="Amazon VOC")
    if voc_sources.count() >= 2:
        result["voc_overview_provenance"] = True
    else:
        raise CheckFailure("voc_overview_provenance_missing")
    project_files = page.locator(".pa-project-files")
    project_files.wait_for(state="visible", timeout=TIMEOUT_MS)
    project_file_row = page.locator(".pa-project-files li").first
    project_file_row.wait_for(state="visible", timeout=TIMEOUT_MS)
    if page.locator(".pa-project-files li").count() > 0:
        result["project_artifacts_visible"] = True
    else:
        raise CheckFailure("project_artifacts_missing")

    set_stage("samples_navigation")
    page.get_by_role("button", name="查看样本").click()
    page.get_by_role("heading", name="样本信息").wait_for(state="visible", timeout=TIMEOUT_MS)
    page.get_by_role("button", name="返回市场档案").click()
    heading.wait_for(state="visible", timeout=TIMEOUT_MS)
    result["samples_navigation_round_trip"] = True

    set_stage("price_chart")
    chart_button = page.get_by_role("button", name="价格 & 销量 / 链接数")
    if chart_button.count() and chart_button.is_enabled():
        chart_button.click()
        page.locator("svg[aria-label='按实际售价排序的销量面积图与链接数折线图']").wait_for(
            state="visible", timeout=TIMEOUT_MS
        )
        result["price_chart"] = True

    set_stage("image_lightbox")
    image_button = page.locator("button[aria-label^='放大查看']").first
    if image_button.count():
        image_button.click()
        dialog = page.locator(".pa-lightbox [role='dialog'][aria-modal='true']")
        dialog.wait_for(state="visible", timeout=TIMEOUT_MS)
        page.get_by_role("button", name="关闭图片预览").click()
        dialog.wait_for(state="hidden", timeout=TIMEOUT_MS)
        result["image_lightbox"] = True

    set_stage("label_slicer")
    slicer_select = page.locator(".pa-tag-slicers select").first
    if slicer_select.count():
        options = slicer_select.locator("option").all()
        choice = next((option.get_attribute("value") for option in options
                       if option.get_attribute("value")), None)
        if choice:
            before = metric_signature(page)
            slicer_select.select_option(choice)
            page.get_by_text("档案指标已按 Data X 标签重新计算", exact=True).wait_for(
                state="visible", timeout=TIMEOUT_MS
            )
            filtered = metric_signature(page)
            if filtered == before:
                # A label may cover the full sample. The filter can still be
                # exercised; the post-clear signature is checked below.
                pass
            clear_button = page.get_by_role("button", name="清除切片")
            clear_button.click()
            expect(clear_button).to_be_disabled(timeout=TIMEOUT_MS)
            if metric_signature(page) != before:
                raise CheckFailure("metrics_not_restored_after_clear")
            result["label_slicer_round_trip"] = True
    else:
        pending = page.locator(".pa-tag-pending")
        pending.wait_for(state="visible", timeout=TIMEOUT_MS)
        if "Data X" not in pending.inner_text():
            raise CheckFailure("missing_tags_state_not_explained")
        result["label_slicer_round_trip"] = "not_applicable_no_data_x_tags"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--dog-archive-id", default=DEFAULT_DOG_ARCHIVE_ID)
    parser.add_argument("--swimwear-archive-id", default="")
    parser.add_argument("--output", default="", help="optional path for the sanitized smoke result JSON")
    args = parser.parse_args()

    parsed = urlparse(args.base_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.port):
        print(json.dumps({"status": "blocked", "reason": "base_url_must_be_local_http"}))
        return 2

    try:
        swimwear_id = args.swimwear_archive_id or discover_swimwear_archive(args.base_url)
    except CheckFailure as exc:
        print(json.dumps({"status": "not_run", "reason": str(exc)}))
        return 2

    archive_ids = [("dog", args.dog_archive_id), ("swimwear", swimwear_id)]
    summary = {
        "status": "passed",
        "base_origin": f"{parsed.scheme}://{parsed.netloc}",
        "external_requests_blocked": True,
        "archives": {},
    }
    blocked_requests = 0
    page_errors = [0]
    stage = ["browser_startup"]

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()

            def route_request(route):
                nonlocal blocked_requests
                target = urlparse(route.request.url)
                if (target.scheme == "http" and target.hostname == parsed.hostname
                        and target.port == parsed.port):
                    route.continue_()
                else:
                    blocked_requests += 1
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda _error: page_errors.__setitem__(0, page_errors[0] + 1))
            for name, archive_id in archive_ids:
                set_stage = lambda step, archive_name=name: stage.__setitem__(
                    0, f"archive_{archive_name}_{step}"
                )
                summary["archives"][name] = check_archive(page, args.base_url, archive_id, set_stage)
            context.close()
            browser.close()
    except CheckFailure as exc:
        summary["status"] = "failed"
        summary["reason"] = str(exc)
    except PlaywrightTimeoutError:
        summary["status"] = "failed"
        summary["reason"] = f"ui_timeout_at_{stage[0]}"
    except Exception as exc:  # Report only class name; exception text may contain page data.
        summary["status"] = "failed"
        summary["reason"] = f"runtime_{type(exc).__name__}_at_{stage[0]}"
    summary["external_requests_blocked_count"] = blocked_requests
    summary["page_error_count"] = page_errors[0]
    if blocked_requests:
        summary["status"] = "failed"
        summary["reason"] = "external_request_attempted"
    if page_errors[0] and summary["status"] == "passed":
        summary["status"] = "failed"
        summary["reason"] = "page_error"
    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized + "\n")
    print(serialized)
    return 0 if summary["status"] == "passed" else 1
if __name__ == "__main__":
    sys.exit(main())
