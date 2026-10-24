"""Read-only DEV browser check for the Product Archive market analysis page.

Only GET requests to the loopback DEV API are allowed. No model or business
task can be submitted by this script.
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5174")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    findings = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        for width in (640, 850, 1440):
            context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
            page = context.new_page()
            errors = []
            blocked = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_handler(route):
                parsed = urlparse(route.request.url)
                if parsed.hostname not in {"127.0.0.1", "localhost"}:
                    blocked.append(route.request.url)
                    route.abort()
                elif parsed.path.startswith("/api/") and route.request.method != "GET":
                    blocked.append(f"{route.request.method} {parsed.path}")
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_handler)
            page.goto(f"{args.base_url.rstrip('/')}/#/product-archive/{args.archive_id}", wait_until="domcontentloaded")
            page.locator("main.pa-dossier h1").wait_for(timeout=20_000)
            page.locator(".ma-legacy").wait_for(timeout=20_000)
            assert page.locator(".pa-project-files").count() == 0
            page.screenshot(path=str(args.output_dir / f"market-layout-{width}.png"), full_page=True)
            page.locator(".ma-legacy").scroll_into_view_if_needed()
            page.screenshot(path=str(args.output_dir / f"market-layout-legacy-{width}.png"))
            widths = page.evaluate("""() => ({
                document: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth,
                main: document.querySelector('main.pa-dossier').scrollWidth,
                mainClient: document.querySelector('main.pa-dossier').clientWidth
            })""")
            overflow = page.evaluate("""() => {
                const main = document.querySelector('main.pa-dossier');
                const edge = main.getBoundingClientRect().right;
                return [...main.querySelectorAll('*')].map(node => ({node, rect: node.getBoundingClientRect()}))
                  .filter(({rect}) => rect.width > 0 && rect.right > edge + 2)
                  .slice(0, 12).map(({node, rect}) => ({
                    tag: node.tagName.toLowerCase(), className: String(node.className || '').slice(0, 90),
                    text: (node.textContent || '').trim().slice(0, 60), right: Math.round(rect.right), edge: Math.round(edge)
                  }));
            }""")
            wide_boxes = page.evaluate("""() => [...document.querySelectorAll('main.pa-dossier *')]
              .filter(node => node.scrollWidth > node.clientWidth + 8 && node.clientWidth > 0)
              .sort((a, b) => (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth))
              .slice(0, 15).map(node => ({
                tag: node.tagName.toLowerCase(), className: String(node.className || '').slice(0, 90),
                scroll: node.scrollWidth, client: node.clientWidth,
                overflowX: getComputedStyle(node).overflowX,
                text: (node.textContent || '').trim().slice(0, 45)
              }))""")
            findings.append({
                "width": width,
                "title": page.locator("main.pa-dossier h1").inner_text(),
                "legacy_segment_groups": page.locator(".ma-legacy-context").count(),
                "legacy_item_count": page.locator(".ma-legacy-context article, .ma-legacy-custom article").count(),
                "analysis_source_summary": page.locator(".pa-analysis-sources").count(),
                "draft_preview_height": page.locator(".overview-suggestion-preview").evaluate("node => Math.round(node.getBoundingClientRect().height)") if page.locator(".overview-suggestion-preview").count() else 0,
                "old_draft_open": page.locator(".ma-old-draft-disclosure").evaluate("node => node.open") if page.locator(".ma-old-draft-disclosure").count() else None,
                "keyword_count": page.locator(".ma-keywords li").count(),
                "widths": widths,
                "overflowing_elements": overflow,
                "wide_boxes": wide_boxes,
                "errors": errors,
                "blocked": blocked,
            })
            context.close()
        browser.close()
    print(json.dumps(findings, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
