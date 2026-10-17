"""Exercise Product Archive import and guarded downstream UI using an isolated package.

The keyword and Reviews dispatch POSTs are intercepted in the browser and answered
with synthetic responses. No downstream job reaches the application or an MCP service.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TIMEOUT_MS = 12_000


class CheckFailure(Exception):
    pass


def create_fixture(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"
    sheet.append([
        "ASIN", "商品详情链接", "产品名称", "品牌", "月销量", "价格", "销售额",
        "卖家国家/地区", "配送方式", "评分数", "评分星级", "上架时间",
    ])
    for index in range(4):
        asin = f"B0TST{index:05d}"
        child_asin = f"B0CHD{index:05d}"
        units = 10 + index * 5
        price = 19.99 + index
        sheet.append([
            asin, f"https://www.amazon.com/dp/{child_asin}", f"Synthetic product {index + 1}",
            f"Synthetic brand {index % 2 + 1}", units, price, units * price,
            "US", "FBA", index * 20, 4.0 + index / 10, "2025-06-01",
        ])
    workbook.save(path)
    workbook.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        print(json.dumps({"status": "blocked", "reason": "base_url_must_be_local_http"}))
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = output.with_name("synthetic-product-sample.xlsx")
    create_fixture(fixture)
    summary: dict = {
        "status": "failed",
        "base_origin": f"{parsed.scheme}://{parsed.netloc}",
        "external_requests_blocked": True,
        "external_request_count": 0,
        "checks": {},
        "dispatch_interceptions": {},
    }
    stage = "browser_startup"
    captures: dict[str, dict] = {}

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()

            def route_request(route):
                target = urlparse(route.request.url)
                local = (target.scheme == "http" and target.hostname == parsed.hostname
                         and target.port == parsed.port)
                if not local:
                    summary["external_request_count"] += 1
                    route.abort()
                    return
                path = target.path
                request = route.request
                if request.method == "GET" and path == "/api/amazon-reviews/settings":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "default_model": "synthetic/provider",
                        "providers": [{"name": "synthetic", "primary_credential_id": "test-only"}],
                    }))
                    return
                if request.method == "POST" and path.startswith("/api/keyword-research/from-product-archive/"):
                    captures["keywords"] = json.loads(request.post_data or "{}")
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "runs": [{"run_id": "synthetic-keyword-task"}],
                    }))
                    return
                if request.method == "POST" and path.startswith("/api/amazon-reviews/from-product-archive/"):
                    captures["reviews"] = json.loads(request.post_data or "{}")
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "id": "synthetic-review-task", "status": "created",
                    }))
                    return
                if request.method == "POST" and (
                    ("keyword-research" in path and "archive" in path)
                    or ("amazon-reviews" in path and "archive" in path)
                ):
                    summary["unexpected_dispatch"] = path
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            page.on("pageerror", lambda _error: summary.__setitem__("page_error", True))

            stage = "open_archive_list"
            page.goto(f"{args.base_url}/#/product-archive", wait_until="domcontentloaded")
            page.get_by_role("heading", name="选品档案").wait_for(state="visible", timeout=TIMEOUT_MS)

            stage = "create_project_and_import"
            import_card = page.locator(".pa-start-card").nth(1)
            import_card.get_by_placeholder("例如：泳衣选品").fill("Synthetic UI Flow")
            import_card.get_by_label("站点").select_option("US")
            import_card.get_by_placeholder("例如：女童泳装").fill("Synthetic category")
            import_card.locator("input[type=file]").set_input_files(str(fixture))
            import_card.get_by_role("button", name="导入并确认映射").click()
            page.get_by_role("heading", name="样本信息").wait_for(state="visible", timeout=TIMEOUT_MS)
            summary["checks"]["synthetic_project_and_import"] = True

            stage = "tagging_dispatch_requires_mapping"
            page.get_by_role("button", name="商品打标").click()
            project_filter = page.get_by_label("项目筛选")
            project_filter.wait_for(state="visible", timeout=TIMEOUT_MS)
            project_filter.select_option(label="Synthetic UI Flow")
            page.locator(".product-table tbody tr").first.wait_for(state="visible", timeout=TIMEOUT_MS)
            row_checks = page.locator(".product-table tbody input[type=checkbox]")
            if row_checks.count() < 2:
                raise CheckFailure("synthetic_tagging_rows_missing")
            row_checks.nth(0).check()
            row_checks.nth(1).check()
            page.get_by_role("button", name="推送关键词调研").click()
            page.get_by_text("样本信息").wait_for(state="visible", timeout=TIMEOUT_MS)
            page.get_by_text("已为当前数据集建立选品档案", exact=False).wait_for(
                state="visible", timeout=TIMEOUT_MS,
            )
            summary["checks"]["mapping_gate_before_dispatch"] = True

            stage = "confirm_field_mapping"
            mapping = {
                "ASIN映射": "ASIN",
                "商品详情链接映射": "商品详情链接",
                "产品名称映射": "产品名称",
                "品牌映射": "品牌",
                "月销量映射": "月销量",
                "销售额映射": "销售额",
                "价格映射": "价格",
                "卖家国家/地区映射": "卖家国家/地区",
                "配送方式映射": "配送方式",
                "评分数映射": "评分数",
                "评分星级映射": "评分星级",
                "上架时间映射": "上架时间",
            }
            for label, option in mapping.items():
                page.get_by_label(label).select_option(label=option)
            page.get_by_role("button", name="确认映射并生成快照").click()
            page.get_by_text("字段映射已确认，指标已按冻结快照计算", exact=True).wait_for(
                state="visible", timeout=TIMEOUT_MS,
            )
            summary["checks"]["field_mapping_confirmed"] = True

            stage = "open_dossier_and_files"
            page.get_by_role("button", name="返回市场档案").click()
            page.locator("main.pa-dossier h1").wait_for(state="visible", timeout=TIMEOUT_MS)
            page.locator(".pa-project-files li").first.wait_for(state="visible", timeout=TIMEOUT_MS)
            if page.locator(".pa-stat-grid").count() < 1:
                raise CheckFailure("market_metrics_missing")
            summary["checks"]["dossier_metrics_and_project_files"] = True

            stage = "project_manager_browse_and_download"
            page.get_by_role("button", name="项目管理").click()
            page.get_by_role("heading", name="项目管理").wait_for(state="visible", timeout=TIMEOUT_MS)
            project_select = page.get_by_label("当前项目")
            project_select.select_option(label="Synthetic UI Flow")
            page.locator(".pm-folder-card").filter(has_text="原始数据").click()
            page.locator(".pm-artifacts .pm-download").first.wait_for(state="visible", timeout=TIMEOUT_MS)
            if "source/raw/" not in page.locator(".pm-artifact").first.inner_text():
                raise CheckFailure("project_file_location_not_shown")
            if page.locator(".pm-download").count() < 1:
                raise CheckFailure("project_file_download_missing")
            summary["checks"]["project_manager_browse_and_download"] = True

            stage = "return_to_tagging_with_selection_preserved"
            page.get_by_role("button", name="商品打标").click()
            page.locator(".product-table tbody tr").first.wait_for(state="visible", timeout=TIMEOUT_MS)
            if "已选 2 行" not in page.locator(".sheet-foot").inner_text():
                raise CheckFailure("selected_rows_not_preserved_after_mapping")
            summary["checks"]["tagging_selection_ready"] = True

            stage = "intercept_keyword_dispatch_from_tagging"
            page.get_by_role("button", name="推送关键词调研").click()
            page.get_by_text("已创建 1 个关键词任务；逐 ASIN 流量矩阵已关闭。", exact=True).wait_for(
                state="visible", timeout=TIMEOUT_MS,
            )
            summary["checks"]["keyword_dispatch_click"] = True

            stage = "intercept_review_dispatch_from_tagging"
            page.get_by_label("评论完成后的 VOC 交接策略").select_option("any_exported")
            page.get_by_role("button", name="启动 Amazon 评论采集").click()
            page.get_by_text("评论采集任务已创建：synthetic-review-task · created", exact=True).wait_for(
                state="visible", timeout=TIMEOUT_MS,
            )
            summary["checks"]["reviews_dispatch_click"] = True

            for kind in ("keywords", "reviews"):
                payload = captures.get(kind)
                if not isinstance(payload, dict):
                    raise CheckFailure(f"{kind}_dispatch_not_intercepted")
                summary["dispatch_interceptions"][kind] = {
                    "product_count": len(payload.get("product_ids") or []),
                    "child_only": payload.get("child_only") is True,
                    "workspace_present": bool(payload.get("workspace_id")),
                    "auto_voc_policy": payload.get("auto_voc_policy"),
                    "never_sent_to_backend": True,
                }
                if len(payload.get("product_ids") or []) != 2 or payload.get("child_only") is not True:
                    raise CheckFailure(f"{kind}_payload_contract_mismatch")
                if kind == "reviews" and payload.get("auto_voc_policy") != "any_exported":
                    raise CheckFailure("reviews_auto_voc_policy_mismatch")

            context.close()
            browser.close()
        if summary.get("external_request_count"):
            raise CheckFailure("external_request_attempted")
        if summary.get("page_error"):
            raise CheckFailure("page_error")
        summary["status"] = "passed"
    except PlaywrightTimeoutError:
        summary["reason"] = f"ui_timeout_at_{stage}"
    except CheckFailure as exc:
        summary["reason"] = str(exc)
    except Exception as exc:
        summary["reason"] = f"runtime_{type(exc).__name__}_at_{stage}"

    summary["fixture"] = "synthetic_four_row_workbook"
    summary["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
