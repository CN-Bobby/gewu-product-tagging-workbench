"""Read-only DEV browser check for Product Archive competition reading layout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    findings = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        for width in (640, 850, 1440):
            context = browser.new_context(
                viewport={"width": width, "height": 900}, service_workers="block"
            )
            page = context.new_page()
            blocked = []
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_handler(route):
                parsed = urlparse(route.request.url)
                if parsed.hostname not in {"127.0.0.1", "localhost"} or (
                    parsed.path.startswith("/api/") and route.request.method != "GET"
                ):
                    blocked.append(f"{route.request.method} {route.request.url}")
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_handler)
            page.goto(
                f"http://127.0.0.1:5174/#/product-archive/{args.archive_id}",
                wait_until="domcontentloaded",
            )
            section = page.locator(".pa-section-competition")
            section.wait_for(timeout=20_000)
            page.locator(".pa-top-asin tbody tr").first.wait_for(timeout=20_000)
            page.locator(".pa-top-tables").screenshot(
                path=str(args.output_dir / f"competition-rankings-{width}.png")
            )
            page.locator(".pa-top-asin").screenshot(
                path=str(args.output_dir / f"competition-asin-{width}.png")
            )
            page.locator(".pa-top-brands").screenshot(
                path=str(args.output_dir / f"competition-brands-{width}.png")
            )
            page.locator(".pa-dist-grid").screenshot(
                path=str(args.output_dir / f"competition-distributions-{width}.png")
            )
            seller_country = page.locator(".pa-dist-grid > .pa-dist").first
            seller_country.screenshot(
                path=str(args.output_dir / f"competition-seller-country-{width}.png")
            )
            assert seller_country.locator(".pa-dist-table tbody tr").count() == 4
            assert seller_country.locator(".pa-dist-table thead th").first.inner_text() == "国家/地区"
            assert "74 行" in seller_country.locator(".pa-missing").inner_text()
            page.locator(".pa-dist-grid > .pa-dist").nth(2).screenshot(
                path=str(args.output_dir / f"competition-distribution-detail-{width}.png")
            )
            rating = page.locator(".pa-dist-grid > .pa-dist").nth(3)
            assert rating.locator(".pa-dist-table thead th").all_text_contents() == [
                "区间", "销量占比", "链接数", "链接占比"
            ]
            assert rating.locator(".pa-dist-table tbody td").first.evaluate(
                "node => parseFloat(getComputedStyle(node).fontSize)"
            ) >= 14
            assert rating.locator(".pa-dist-table thead th").nth(3).evaluate(
                "node => getComputedStyle(node).textAlign"
            ) == "right"
            assert rating.locator(".pa-dist-table tbody tr").first.locator(
                ".pa-bar-track i"
            ).get_attribute("style") == "width: 0%;"
            assert page.locator(".pa-price-dist > .pa-dist").evaluate(
                "node => getComputedStyle(node).borderTopWidth"
            ) == "0px"
            widths = page.evaluate(
                """() => ({
                    document: document.documentElement.scrollWidth,
                    viewport: document.documentElement.clientWidth,
                    main: document.querySelector('main.pa-dossier').scrollWidth,
                    mainClient: document.querySelector('main.pa-dossier').clientWidth
                })"""
            )
            first_table = page.locator(".pa-top-asin .pa-competition-table")
            table_measure = first_table.evaluate(
                """node => ({
                    client: node.clientWidth, scroll: node.scrollWidth,
                    font: getComputedStyle(node.querySelector('td')).fontSize
                })"""
            )
            first_brand = page.locator(".pa-brand-card").first
            assert first_brand.get_attribute("open") is None
            first_brand.locator("summary").press("Enter")
            assert first_brand.get_attribute("open") is not None
            assert first_brand.locator(".pa-brand-images .pa-thumb").count() > 0
            first_brand.locator("summary").press("Enter")
            assert first_brand.get_attribute("open") is None
            first_brand.locator("summary").click()
            assert first_brand.get_attribute("open") is not None
            first_brand.locator("summary").click()
            assert first_brand.get_attribute("open") is None
            if table_measure["scroll"] > table_measure["client"]:
                first_table.focus()
                first_table.press("ArrowRight")
                page.wait_for_timeout(200)
                assert first_table.evaluate("node => node.scrollLeft") > 0
            assert widths["document"] <= widths["viewport"] + 2, widths
            assert widths["main"] <= widths["mainClient"] + 2, widths
            assert not blocked and not errors, (blocked, errors)
            findings.append(
                {
                    "width": width,
                    "widths": widths,
                    "asin_table": table_measure,
                    "brand_count": page.locator(".pa-brand-card").count(),
                    "distribution_count": page.locator(".pa-dist-grid > *").count(),
                    "blocked": blocked,
                    "errors": errors,
                }
            )
            context.close()
        browser.close()
    print(json.dumps(findings, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
