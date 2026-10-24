"""Read-only visual check of the current DEV keyword trend chart; no task starts."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ARCHIVE_ID = "4aee495eece84b4aaf4387a205b74bb8"
URL = f"http://127.0.0.1:5174/#/product-archive/{ARCHIVE_ID}"
SCREENSHOT = Path(r"K:\GeWuDev\Data\product-archive\acceptance-reports\20261023-keyword-seasonality-live.png")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        writes, errors = [], []
        page.on("request", lambda request: writes.append(f"{request.method} {request.url}")
                if "/api/" in request.url and request.method not in {"GET", "HEAD"} else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="domcontentloaded")
        page.get_by_text("查看细分市场分析").click()
        chart = page.locator(".ma-trend")
        chart.get_by_text("关键词搜索需求走势").wait_for()
        chart.get_by_text("US_男士裤装_细分ASIN调研_20261022.xlsx").wait_for()
        assert chart.locator(".ma-trend-legend button[aria-pressed]").count() == 10
        assert chart.locator(".recharts-line").count() == 10
        chart.get_by_role("button", name="原始搜索量").click()
        assert chart.get_by_text("纵轴为报告记录的月搜索量", exact=False).count() == 1
        chart.get_by_role("button", name="相对走势").click()
        widths = {}
        for width in (640, 850, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            chart.scroll_into_view_if_needed()
            page.wait_for_timeout(120)
            state = page.evaluate("""() => ({body:document.documentElement.scrollWidth,
                viewport:window.innerWidth})""")
            assert state["body"] <= state["viewport"] + 1, (width, state)
            widths[width] = state
        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
        page.mouse.move(0, 0)
        chart.screenshot(path=str(SCREENSHOT))
        page.set_viewport_size({"width": 640, "height": 1000})
        page.evaluate("document.documentElement.style.zoom = '2'")
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        assert not writes and not errors, (writes, errors)
        print(json.dumps({"keywords": 10, "months": 24, "widths": widths,
                          "zoom": "200%", "api_writes": writes, "page_errors": errors,
                          "screenshot": str(SCREENSHOT)}, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
