"""Exercise v2 archive review and editing against isolated browser fixtures.

Only the DEV frontend shell is loaded. Every API response is synthetic and all
external requests are blocked. This script never starts a model or business job.
"""

from __future__ import annotations

import copy
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from test_product_archive_deep_research_browser import (
    ARCHIVE_ID, RUN_ID, BrowserContract, local_http_origin,
    open_research_page, response_body,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.product_archive import service as archive_service


BASE_URL = "http://127.0.0.1:5174"


def fixture_analysis() -> dict:
    common = {"classification": "supported", "basis": "observation",
              "basis_note": "Synthetic tagged rows and reviews", "limits": "Sample only",
              "evidence_refs": ["src-tags"]}
    return {
        "schema_version": 2,
        "overview": {"text": "休闲裤包含不同商品形态，服务于不同工作与通勤任务。",
                     **(common | {"evidence_refs": ["web-form"]})},
        "product_forms": [
            {"id": "F1", "name": "工装裤", "description": "工地班次携带工具并活动时穿着的多口袋长裤。", **common},
            {"id": "F2", "name": "斜纹休闲裤", "description": "通勤和非正式办公穿着的简洁长裤。", **common},
        ],
        "demand_contexts": [
            {"id": "C1", "name": "工地班次", "product_form_ids": ["F1"],
             "audience": "施工人员", "trigger": "开始长时间户外班次", "task": "携带工具并移动",
             "obstacles": "口袋不足和活动受限", "motivation": "减少取用工具的阻碍",
             "desired_outcome": "工作中活动自如", "product_response": "口袋与弹力面料回应任务",
             "conditions": "户外和高温", "boundary": "制服要求可能限制使用", **common},
            {"id": "C2", "name": "日常通勤", "product_form_ids": ["F2"],
             "audience": "通勤者", "trigger": "日常出门", "task": "在通勤和办公室切换",
             "obstacles": "过于休闲的外观", "motivation": "兼顾得体与舒适",
             "desired_outcome": "全天使用", "product_response": "简洁轮廓回应服装要求",
             "conditions": "普通办公", "boundary": "不适合正式商务", **common},
        ],
        "cross_cutting": [
            {"id": "X1", "kind": "fit", "title": "跨形态的大腿围适配",
             "text": "两种形态均可能出现腰围合适但腿部紧绷。",
             "product_form_ids": ["F1", "F2"], "context_ids": ["C1", "C2"], **common},
        ],
        "needs_validation": [{"id": "V1", "text": "核对不同体型的尺码反馈。",
                              "product_form_ids": ["F1"], "context_ids": ["C1"]}],
    }


class V2Contract(BrowserContract):
    def __init__(self, decision: str):
        super().__init__(BASE_URL, local_http_origin(BASE_URL), "success")
        self.decision = decision
        self.analysis = fixture_analysis()
        self.current_archive["updated_at"] = "v1"
        self.current_archive["market_analysis"] = None
        self.review_payloads: list[dict] = []
        self.reviewed = False

    def handle(self, route) -> None:
        request = route.request
        path = urlparse(request.url).path
        prefix = f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts"
        if request.method == "GET" and path == f"{prefix}/latest-open":
            value = {"id": None} if self.reviewed else {"id": RUN_ID, "status": "ready"}
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        if request.method == "GET" and path == f"{prefix}/{RUN_ID}":
            value = self.expected_run("ready", "ready", model_attempted=True)
            value["analysis_version"] = "market-analysis-v2-reasonix"
            value["market_analysis"] = copy.deepcopy(self.analysis)
            value["suggestions"] = {}
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        if request.method == "POST" and path == f"{prefix}/{RUN_ID}/review":
            payload = json.loads(request.post_data or "{}")
            self.review_payloads.append(payload)
            if payload.get("expected_updated_at") != self.current_archive["updated_at"]:
                route.fulfill(status=400, content_type="application/json",
                              body=response_body({"detail": "档案内容已被其他操作更新，请刷新后重试"}))
                return
            self.reviewed = True
            if payload["decision"] == "accepted":
                try:
                    archive_service._validate_market_analysis(payload["market_analysis"])
                except ValueError as error:
                    route.fulfill(status=422, content_type="application/json",
                                  body=response_body({"detail": str(error)}))
                    return
                self.current_archive["market_analysis"] = copy.deepcopy(payload["market_analysis"])
                self.current_archive["market_analysis_run_id"] = RUN_ID
                self.current_archive["updated_at"] = "v2"
            route.fulfill(status=200, content_type="application/json",
                          body=response_body({"draft": {"review_decision": payload["decision"]},
                                              "archive": self.current_archive}))
            return
        if request.method == "PATCH" and path == f"/api/product-archive/archives/{ARCHIVE_ID}":
            payload = json.loads(request.post_data or "{}")
            self.patch_payloads.append(payload)
            if payload.get("expected_updated_at") != self.current_archive["updated_at"]:
                route.fulfill(status=400, content_type="application/json",
                              body=response_body({"detail": "档案内容已被其他操作更新，请刷新后重试"}))
                return
            if "market_analysis" in payload:
                try:
                    archive_service._validate_market_analysis(payload["market_analysis"])
                except ValueError as error:
                    route.fulfill(status=422, content_type="application/json",
                                  body=response_body({"detail": str(error)}))
                    return
            self.current_archive.update(payload)
            self.current_archive["updated_at"] = "v3"
            route.fulfill(status=200, content_type="application/json", body=response_body(self.current_archive))
            return
        super().handle(route)


def run_case(browser, decision: str, output_dir: Path | None = None) -> dict:
    contract = V2Contract(decision)
    context = browser.new_context(viewport={"width": 1100, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_text("整份分析已准备好").wait_for()
    assert page.get_by_text("查看商品形态与需求情境").count() == 1
    sources = page.locator(".pa-analysis-sources")
    assert sources.count() == 1
    assert sources.locator("li").count() == 2
    assert sources.get_by_text("Synthetic tagging workbook").count() == 1
    assert sources.get_by_text("Synthetic keyword results").count() == 1
    assert sources.get_by_text("Synthetic review results").count() == 0
    assert page.locator(".pa-project-files").count() == 0
    if decision == "accepted":
        assert page.locator("details.ma-pending").get_attribute("open") is not None
        page.get_by_text("工地班次", exact=True).first.wait_for()
        page.get_by_role("button", name="保留整份分析").click()
        page.get_by_role("button", name="编辑分析").wait_for()
        assert page.locator(".pa-analysis-sources li").count() == 2
        assert contract.current_archive["market_analysis"]["product_forms"][0]["id"] == "F1"
        page.get_by_role("button", name="编辑分析").click()
        page.get_by_role("button", name="＋ 添加形态").click()
        assert page.locator(".ma-form").count() == 3
        page.locator(".ma-form").last.get_by_label("形态名称").fill("运动休闲裤")
        page.get_by_role("button", name="＋ 添加情境").click()
        assert page.locator(".ma-context").count() == 3
        page.locator(".ma-context").last.get_by_label("情境名称").fill("周末散步")
        page.locator(".ma-context").last.locator(".ma-associations").get_by_label("运动休闲裤").check()
        page.get_by_role("button", name="删除商品形态 工装裤").click()
        assert page.locator(".ma-form").count() == 2
        page.get_by_role("button", name="删除需求情境 工地班次").click()
        assert page.locator(".ma-context").count() == 2
        page.get_by_role("button", name="保存概况").click()
        page.get_by_text("档案概况已保存").wait_for()
        saved = contract.current_archive["market_analysis"]
        assert [x["name"] for x in saved["product_forms"]] == ["斜纹休闲裤", "运动休闲裤"]
        assert [x["name"] for x in saved["demand_contexts"]] == ["日常通勤", "周末散步"]
        assert saved["demand_contexts"][1]["product_form_ids"] == [saved["product_forms"][1]["id"]]
        assert saved["needs_validation"][0]["text"] == "核对不同体型的尺码反馈。"
        assert saved["needs_validation"][0]["product_form_ids"] == []
        assert saved["needs_validation"][0]["context_ids"] == []
        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name="编辑分析").wait_for()
        page.locator(".pa-analysis-sources li").first.wait_for()
        assert page.locator(".pa-analysis-sources li").count() == 2
        assert page.get_by_text("运动休闲裤", exact=True).count() > 0
        page.get_by_text("Synthetic product forms", exact=False).first.wait_for()
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            page.locator(".ma-overview").scroll_into_view_if_needed()
            page.screenshot(path=str(output_dir / "market-v2-overview-1100.png"))
            page.locator(".ma-contexts").scroll_into_view_if_needed()
            page.screenshot(path=str(output_dir / "market-v2-contexts-1100.png"))
    else:
        initial = copy.deepcopy(contract.current_archive)
        page.get_by_role("button", name="忽略整份分析").click()
        page.get_by_text("已整体忽略分析").wait_for()
        assert contract.current_archive == initial
        assert page.get_by_role("button", name="编辑分析").count() == 0
        assert page.locator(".pa-analysis-sources").count() == 0
    assert len(contract.review_payloads) == 1
    assert contract.review_payloads[0]["decision"] == decision
    assert contract.blocked_external == 0
    assert contract.blocked_unknown_api == []
    assert contract.page_errors == 0
    result = {"decision": decision, "review_requests": len(contract.review_payloads),
              "save_requests": len(contract.patch_payloads), "page_errors": contract.page_errors,
              "unknown_api": contract.blocked_unknown_api,
              "forms_after": len((contract.current_archive.get("market_analysis") or {}).get("product_forms", []))}
    context.close()
    return result


def run_conflict_case(browser) -> dict:
    contract = V2Contract("accepted")
    context = browser.new_context(viewport={"width": 1100, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_role("button", name="保留整份分析").wait_for()
    contract.current_archive["updated_at"] = "changed-in-other-tab"
    page.get_by_role("button", name="保留整份分析").click()
    page.get_by_text("审核冲突", exact=False).wait_for()
    assert contract.current_archive["market_analysis"] is None
    assert contract.reviewed is False
    assert len(contract.review_payloads) == 1
    assert contract.page_errors == 0
    context.close()
    return {"decision": "conflict", "review_requests": 1, "archive_unchanged": True}


def run_saved_layout(browser, width: int, output_dir: Path | None = None) -> dict:
    contract = V2Contract("accepted")
    contract.reviewed = True
    contract.current_archive["market_analysis"] = fixture_analysis()
    contract.current_archive["market_analysis_run_id"] = RUN_ID
    context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_role("button", name="编辑分析").wait_for()
    page.locator(".pa-analysis-sources li").first.wait_for()
    assert page.locator(".pa-analysis-sources li").count() == 2
    page.locator(".ma-contexts").scroll_into_view_if_needed()
    widths = page.evaluate("""() => ({
      page: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
      main: document.querySelector('main.pa-dossier').scrollWidth,
      mainClient: document.querySelector('main.pa-dossier').clientWidth
    })""")
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_dir / f"market-v2-contexts-{width}.png"))
    assert widths["page"] <= widths["viewport"]
    assert widths["main"] <= widths["mainClient"]
    assert contract.page_errors == 0
    context.close()
    return {"saved_width": width, "widths": widths, "page_errors": contract.page_errors}


def run_legacy_edit_case(browser) -> dict:
    contract = V2Contract("ignored")
    contract.reviewed = True
    contract.current_archive["overview_modules_initialized"] = True
    contract.current_archive["overview_modules"] = [
        {"id": "module-product_function", "field_key": "product_function", "title": "产品与功能",
         "items": [{"id": "old-function", "text": "旧产品说明", "segment_id": "S1",
                    "segment_name": "旧细分"}]},
    ]
    context = browser.new_context(viewport={"width": 1100, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_text("旧版分析未保存资料快照", exact=False).wait_for()
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator(".ma-legacy-editor summary").click()
    page.get_by_role("button", name="编辑旧档案内容").click()
    page.get_by_label("自定义模块名称").fill("购买顾虑")
    page.get_by_role("button", name="新增自定义模块").click()
    first_module = page.locator(".ma-legacy-editor article").first
    first_module.get_by_role("button", name="＋ 新增一条内容").click()
    first_module.locator(".ma-legacy-item-editor").last.locator("textarea").fill("人工新增说明")
    page.get_by_role("button", name="保存概况").click()
    page.get_by_text("档案概况已保存").wait_for()
    saved_modules = contract.current_archive["overview_modules"]
    assert any(x["title"] == "购买顾虑" for x in saved_modules)
    assert any(item["text"] == "人工新增说明" for item in saved_modules[0]["items"])
    page.reload(wait_until="domcontentloaded")
    page.locator(".ma-legacy-editor summary").click()
    page.get_by_role("button", name="编辑旧档案内容").click()
    page.get_by_role("button", name="删除自定义模块 购买顾虑").click()
    page.get_by_role("button", name="保存概况").click()
    page.get_by_text("档案概况已保存").wait_for()
    assert all(x["title"] != "购买顾虑" for x in contract.current_archive["overview_modules"])
    assert contract.page_errors == 0
    context.close()
    return {"legacy_edit": True, "save_requests": len(contract.patch_payloads),
            "custom_module_removed": True, "page_errors": contract.page_errors}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            result = [run_case(browser, "accepted", args.output_dir), run_case(browser, "ignored"),
                      run_conflict_case(browser),
                      *[run_saved_layout(browser, width, args.output_dir) for width in (640, 850, 1440)],
                      run_legacy_edit_case(browser)]
        finally:
            browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
