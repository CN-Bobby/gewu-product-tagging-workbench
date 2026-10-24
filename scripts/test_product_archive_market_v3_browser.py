"""Render v3 segment cards with isolated API fixtures; no model or business jobs."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from test_product_archive_deep_research_browser import ARCHIVE_ID, RUN_ID, open_research_page, response_body
from test_product_archive_market_v2_browser import BASE_URL, V2Contract

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.product_archive.market_analysis_v3 import validate


def claim(text, **extra):
    return {"text": text, "classification": "hypothesis", "basis": "inference",
            "basis_note": "隔离测试中的示例推断。", "evidence_refs": [], "limits": "仅用于界面测试。", **extra}


def fixture_analysis():
    def segment(ident, name, product, audience, scenario):
        return {"id": ident, "name": name, "fields": {
            "product": {"items": [claim(product)], "missing_reason": ""},
            "audience": {"items": [claim(audience)], "missing_reason": ""},
            "scenarios": {"items": [claim(scenario)], "missing_reason": ""},
            "culture": {"items": [], "missing_reason": "目前未发现足以判断的特定文化因素。"},
            "seasonality": {"items": [claim("推断全年有基础需求，气候影响面料选择；销售旺季待验证。",
                                             kind="possible_use_period", measured_months=[])], "missing_reason": ""},
        }, "needs_validation": [{"id": f"V-{ident}", "text": "需要跨月需求数据核验旺季。"}]}
    return {"schema_version": 3,
            "overview": claim("当前样本呈现作业型与日常型两条需求线索。"),
            "segments": [segment("S1", "蓝领技工作业裤", "宽松弹力斜纹 cargo 帮助作业者蹲跪与携物；不适合正式办公。",
                                 "建筑和仓储作业者，家人可能代购。", "工作班次中蹲跪、布线并随身携带工具。"),
                         segment("S2", "日常通勤休闲裤", "直筒斜纹裤兼顾通勤得体与舒适；不适合正式商务。",
                                 "普通办公和通勤的使用者。", "工作日通勤与非正式办公。")],
            "needs_validation": []}


class V3Contract(V2Contract):
    def __init__(self, decision):
        super().__init__(decision)
        self.analysis = fixture_analysis()

    def handle(self, route):
        path = urlparse(route.request.url).path
        prefix = f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts"
        if route.request.method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/keyword-seasonality":
            months = ([f"2024-{month:02d}" for month in range(10, 13)] +
                      [f"2025-{month:02d}" for month in range(1, 13)] +
                      [f"2026-{month:02d}" for month in range(1, 10)])
            value = {"status": "available", "source": {"id": "src-keywords", "name": "Synthetic keyword results"},
                     "marketplace": "US", "metric": "monthly_searches", "months": months,
                     "keywords": [{"keyword": f"keyword {i + 1}", "translation": f"词 {i + 1}",
                                    "type": "核心" if i < 5 else "长尾",
                                    "values": [100 + i * 40 + month * (i + 2) for month in range(24)]}
                                   for i in range(10)]}
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        if route.request.method == "GET" and path == f"{prefix}/{RUN_ID}":
            value = self.expected_run("ready", "ready", model_attempted=True)
            value.update(analysis_version="market-analysis-v3-reasonix",
                         market_analysis=copy.deepcopy(self.analysis), suggestions={})
            route.fulfill(status=200, content_type="application/json", body=response_body(value))
            return
        super().handle(route)


def check_width(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.wait_for_timeout(80)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    assert not overflow, f"horizontal overflow at {width}px"
    pending_width = page.locator(".ma-pending").evaluate("el => el.getBoundingClientRect().width")
    assert pending_width >= {640: 400, 850: 500, 1440: 700}[width], (
        f"analysis panel collapsed into a narrow outer-grid column at {width}px: {pending_width}px"
    )
    assert page.locator(".ma-v3-card").count() == 2
    assert page.locator(".ma-v3-card").first.locator(".ma-v3-field").count() == 5


def run_case(browser, decision, screenshot=None):
    contract = V3Contract(decision)
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    assert page.locator("details.ma-pending").get_attribute("open") is not None
    page.locator(".ma-v3-card").first.get_by_text("蓝领技工作业裤", exact=True).wait_for()
    chart = page.locator(".ma-trend")
    chart.get_by_text("关键词搜索需求走势").wait_for()
    chart.locator(".recharts-line").first.wait_for()
    assert chart.locator(".ma-trend-legend button[aria-pressed]").count() == 10
    assert chart.locator(".recharts-line").count() == 10
    chart.get_by_role("button", name="原始搜索量").click()
    assert chart.get_by_text("纵轴为报告记录的月搜索量", exact=False).count() == 1
    chart.locator(".ma-trend-legend button[aria-pressed]").first.click()
    assert chart.locator(".recharts-line").count() == 1
    chart.get_by_role("button", name="显示全部").click()
    assert chart.locator(".recharts-line").count() == 10
    for width in (640, 850, 1440):
        check_width(page, width)
    details = page.locator(".ma-v3-card-evidence").first
    details.locator("summary").focus()
    page.keyboard.press("Enter")
    assert details.get_attribute("open") is not None
    details.locator("summary").press("Enter")
    page.locator(".ma-v3-lines p").first.evaluate(
        "el => { el.textContent = '很长的结论文字'.repeat(70) + 'unbroken_product_configuration_'.repeat(15) }")
    page.set_viewport_size({"width": 640, "height": 900})
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    page.evaluate("document.documentElement.style.zoom = '2'")
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    page.evaluate("document.documentElement.style.zoom = ''")
    if decision == "ignored":
        page.get_by_role("button", name="忽略整份分析").click()
        assert contract.current_archive["market_analysis"] is None
    else:
        page.get_by_role("button", name="保留整份分析").click()
        page.get_by_role("button", name="编辑分析").wait_for()
        validate(contract.current_archive["market_analysis"])
        page.get_by_role("button", name="编辑分析").click()
        page.get_by_role("button", name="＋ 添加细分").click()
        card = page.locator(".ma-v3-card").last
        card.get_by_label("细分名称").fill("运动休闲裤")
        for label, text in (("产品是什么", "弹力针织裤用于轻活动。"),
                            ("谁使用", "周末轻活动的成年人。"),
                            ("使用情境", "散步或短途出行。")):
            card.get_by_role("button", name=f"＋ 添加{label}").click()
            card.get_by_label(f"{label} 1").fill(text)
        card.get_by_role("button", name="上移运动休闲裤").click()
        assert page.locator(".ma-v3-card").nth(1).get_by_label("细分名称").input_value() == "运动休闲裤"
        page.get_by_role("button", name="保存概况").click()
        page.get_by_text("档案概况已保存").wait_for()
        saved = contract.current_archive["market_analysis"]
        validate(saved)
        assert [item["name"] for item in saved["segments"]] == ["蓝领技工作业裤", "运动休闲裤", "日常通勤休闲裤"]
        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name="编辑分析").wait_for()
        page.locator(".ma-trend").wait_for()
        assert page.locator(".ma-trend").count() == 1
        assert page.get_by_text("运动休闲裤", exact=True).count()
        if screenshot:
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator(".ma-v3-card").first.scroll_into_view_if_needed()
            page.screenshot(path=str(screenshot), full_page=False)
    context.close()
    return {"decision": decision, "review_requests": len(contract.review_payloads),
            "segments_saved": len(contract.current_archive.get("market_analysis", {}).get("segments", []))
            if contract.current_archive.get("market_analysis") else 0}


def check_pending_and_saved_share_one_chart(browser):
    contract = V3Contract("accepted")
    contract.current_archive["market_analysis"] = fixture_analysis()
    contract.current_archive["market_analysis_run_id"] = RUN_ID
    context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
    page = open_research_page(context, BASE_URL, contract)
    assert page.locator(".ma-generate-disclosure").get_attribute("open") is not None
    assert page.locator(".ma-pending").get_attribute("open") is not None
    page.locator(".ma-trend").wait_for()
    assert page.locator(".ma-v3-card").count() == 4
    assert page.locator(".ma-trend").count() == 1
    context.close()
    return {"pending_and_saved": "one_shared_chart"}


if __name__ == "__main__":
    screenshot = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        print(json.dumps([run_case(browser, "accepted", screenshot), run_case(browser, "ignored"),
                          check_pending_and_saved_share_one_chart(browser)], ensure_ascii=False))
        browser.close()
