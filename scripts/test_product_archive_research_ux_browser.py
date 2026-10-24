"""Check the archive research journey against isolated Product Archive APIs.

The DEV server only supplies frontend assets. All API calls are intercepted by
the synthetic contracts, and non-loopback requests are blocked. No model or
business job is started by this script.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from test_product_archive_deep_research_browser import (
    ARCHIVE_ID, RUN_ID, BrowserContract, local_http_origin, open_research_page,
    response_body,
)
from test_product_archive_market_v3_browser import (
    BASE_URL, V3Contract, fixture_analysis,
)


class RecoveryContract(V3Contract):
    """Return a failed run that can be recovered without a new model request."""

    def __init__(self):
        super().__init__("accepted")
        self.recovered = False
        self.recovery_requests = 0

    def handle(self, route):
        path = urlparse(route.request.url).path
        prefix = f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts"
        if route.request.method == "GET" and path == f"{prefix}/{RUN_ID}" and not self.recovered:
            value = self.expected_run("failed", "model_analysis", model_attempted=True,
                                      error="Synthetic model output could not be parsed.")
            value["recovery"] = {"available": True, "reason": ""}
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        if route.request.method == "POST" and path == f"{prefix}/{RUN_ID}/recover":
            self.recovery_requests += 1
            self.recovered = True
            value = self.expected_run("ready", "ready", model_attempted=True)
            value.update(analysis_version="market-analysis-v3-reasonix",
                         market_analysis=copy.deepcopy(self.analysis), suggestions={})
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        super().handle(route)


class RunningContract(BrowserContract):
    """Hold the synthetic task in progress while its running UI is inspected."""

    def handle(self, route):
        path = urlparse(route.request.url).path
        if (route.request.method == "GET"
                and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts/{RUN_ID}"):
            value = self.expected_run("running", "public_research", model_attempted=True)
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        super().handle(route)


class ScopedV3Contract(V3Contract):
    def expected_run(self, *args, **kwargs):
        value = super().expected_run(*args, **kwargs)
        value["research_scope"] = {"category_context": "Girls swim shirt US"}
        return value


class PreflightFailureContract(BrowserContract):
    def handle(self, route):
        path = urlparse(route.request.url).path
        if (route.request.method == "GET"
                and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts/preflight"):
            self.preflight_reads += 1
            route.fulfill(status=503, content_type="application/json",
                          body=response_body({"detail": "synthetic preflight failure"}))
            return
        super().handle(route)


def assert_local_only(contract):
    assert contract.blocked_external == 0, "browser attempted an external request"
    assert not contract.blocked_unknown_api, contract.blocked_unknown_api
    assert contract.page_errors == 0, "page raised an uncaught error"


def check_start_and_progress(browser):
    contract = RunningContract(BASE_URL, local_http_origin(BASE_URL), "success")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.locator(".pa-research-preflight").wait_for()
    assert page.locator(".pa-overview-generate input[type=month]").count() == 0
    assert page.get_by_role("button", name="开始调研").count() == 1

    scope = page.locator("details.pa-research-setup")
    assert scope.count() == 1
    scope.locator("summary").focus()
    page.keyboard.press("Enter")
    assert scope.get_attribute("open") is not None
    scope.locator("summary").press("Enter")
    assert scope.get_attribute("open") is None

    page.get_by_role("button", name="开始调研").click()
    page.locator(".overview-progress-stage").wait_for(state="visible")
    assert len(contract.create_payloads) == 1
    payload = contract.create_payloads[0]
    assert payload.get("public_research") is True
    assert payload.get("data_month_context") in (None, ""), payload
    assert payload.get("selected_source_ids") == ["src-tags", "src-keywords"]
    assert scope.get_attribute("open") is None, "setup stayed expanded during research"
    assert page.locator(".pa-start-research").is_disabled()
    assert_local_only(contract)
    context.close()
    return {"submitted": len(contract.create_payloads), "month_input": "removed",
            "running": "visible"}


def check_missing_category(browser):
    contract = BrowserContract(BASE_URL, local_http_origin(BASE_URL), "success")
    contract.current_archive["category"] = ""
    context = browser.new_context(viewport={"width": 850, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_text("请先展开研究范围并填写品类").wait_for(state="visible")
    assert page.locator(".pa-start-research").is_disabled()
    assert contract.preflight_reads == 0
    page.locator(".pa-research-setup > summary").click()
    page.get_by_label("研究范围").fill("Girls swim shirt US")
    page.locator(".pa-research-preflight").get_by_text("本次模型", exact=False).wait_for()
    assert not page.locator(".pa-start-research").is_disabled()
    assert contract.preflight_reads == 1
    assert "资料未提供覆盖范围" in page.locator(".pa-research-date-coverage").inner_text()
    assert_local_only(contract)
    context.close()
    return {"empty_category": "clearly_disabled", "scope_override": "enabled"}


def check_preflight_failure(browser):
    contract = PreflightFailureContract(BASE_URL, local_http_origin(BASE_URL), "success")
    context = browser.new_context(viewport={"width": 850, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.get_by_text("输入预估暂不可用", exact=False).wait_for(state="visible")
    assert not page.locator(".pa-start-research").is_disabled()
    assert contract.preflight_reads == 1
    assert not contract.create_payloads
    assert_local_only(contract)
    context.close()
    return {"preflight_failure": "explained", "model_tasks_created": 0}


def check_pending_with_saved(browser):
    contract = ScopedV3Contract("accepted")
    contract.current_archive["market_analysis"] = fixture_analysis()
    contract.current_archive["market_analysis_run_id"] = RUN_ID
    context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    pending = page.locator("details.ma-pending")
    pending.wait_for(state="visible")
    assert pending.get_attribute("open") is not None, "pending report is hidden after reload"
    assert page.locator(".ma-generate-disclosure > summary").inner_text() == "待审核分析"
    assert not page.locator(".pa-research-setup").is_visible(), "inputs obscure the pending result"
    assert page.locator(".pa-research-summary").get_attribute("open") is None
    assert "Girls swim shirt US" in pending.locator(":scope > summary").inner_text()
    decision = page.locator(".pa-analysis-decision")
    assert decision.is_visible(), "the whole-report decision is hidden after reload"
    assert page.locator("details.ma-saved-analysis").count() == 1
    assert page.locator("details.ma-saved-analysis").get_attribute("open") is None
    assert "Girls swim shirt US" in page.locator("details.ma-saved-analysis").text_content()
    assert decision.evaluate("el => getComputedStyle(el).position") != "sticky"
    page.locator(".ma-trend").wait_for(state="visible")
    assert page.locator(".ma-trend").count() == 1

    for width in (640, 850, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), width
        assert pending.is_visible() and decision.is_visible(), width
    page.set_viewport_size({"width": 640, "height": 900})
    page.evaluate("document.documentElement.style.zoom = '2'")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), "200% zoom"
    page.evaluate("document.documentElement.style.zoom = ''")

    page.get_by_role("button", name="保留整份分析").click()
    page.get_by_role("button", name="编辑分析").wait_for(state="visible")
    assert len(contract.review_payloads) == 1
    assert contract.review_payloads[0]["decision"] == "accepted"
    assert_local_only(contract)
    context.close()
    return {"pending_after_reload": "visible", "saved_report": "collapsed",
            "trend_charts": 1, "review": "accepted"}


def check_ignore(browser):
    contract = V3Contract("ignored")
    context = browser.new_context(viewport={"width": 850, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.locator("details.ma-pending").wait_for(state="visible")
    page.get_by_role("button", name="忽略整份分析").click()
    assert contract.current_archive["market_analysis"] is None
    assert len(contract.review_payloads) == 1
    assert contract.review_payloads[0]["decision"] == "ignored"
    assert_local_only(contract)
    context.close()
    return {"review": "ignored"}


def check_failure_and_recovery(browser):
    contract = RecoveryContract()
    context = browser.new_context(viewport={"width": 850, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    page.locator(".overview-failed").wait_for(state="visible")
    page.reload(wait_until="domcontentloaded")
    page.locator(".overview-failed").wait_for(state="visible")
    page.get_by_role("button", name="离线恢复分析结果").click()
    page.locator("details.ma-pending").wait_for(state="visible")
    assert contract.recovery_requests == 1
    assert not contract.create_payloads, "recovery started a new model task"
    assert_local_only(contract)
    context.close()
    return {"failure_after_reload": "visible", "offline_recovery": "ready",
            "model_tasks_created": 0}


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        results = [check_start_and_progress(browser), check_missing_category(browser),
                   check_preflight_failure(browser),
                   check_pending_with_saved(browser),
                   check_ignore(browser), check_failure_and_recovery(browser)]
        browser.close()
    print(json.dumps(results, ensure_ascii=False))
