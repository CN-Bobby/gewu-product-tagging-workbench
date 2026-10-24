"""Exercise one real draft result against a synthetic archive in the DEV browser.

All browser API requests are intercepted. The real run is read once through the
local DEV API; no browser action can patch the user's archive or call a model.
"""
from __future__ import annotations

import argparse
import copy
import json
import urllib.request

from playwright.sync_api import sync_playwright

import test_product_archive_deep_research_browser as base


def read_real_run(api_url: str, archive_id: str, run_id: str) -> dict:
    url = f"{api_url.rstrip('/')}/api/product-archive/archives/{archive_id}/overview-drafts/{run_id}"
    with urllib.request.urlopen(url, timeout=15) as response:
        row = json.load(response)
    if row.get("status") != "ready" or not row.get("suggestions"):
        raise RuntimeError("The selected real run is not ready for review")
    return row


def contract_with_real_result(base_url: str, row: dict, *, baseline: bool = False):
    contract = base.BrowserContract(base_url, base.local_http_origin(base_url), "ready")
    contract.current_archive["updated_at"] = "synthetic-before"
    if baseline:
        contract.current_archive["overview_modules"] = [{
            "id": "module-product_name", "field_key": "product_name",
            "title": "产品概况", "items": [{
                "id": "original-note", "text": "隔离档案原有内容",
                "source_type": "manual", "source_ids": [], "links": [],
            }],
        }]
    original_expected_run = contract.expected_run

    def expected_run(status: str, phase: str, **kwargs):
        if status != "ready":
            return original_expected_run(status, phase, **kwargs)
        answer = copy.deepcopy(row)
        answer.update(id=base.RUN_ID, archive_id=base.ARCHIVE_ID,
                      status="ready", phase="ready")
        return answer

    contract.expected_run = expected_run
    return contract


def drive_accept(browser, base_url: str, row: dict) -> dict:
    contract = contract_with_real_result(base_url, row)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = base.open_research_page(context, base_url, contract)
    base.start_run(page, category="男士休闲裤", month="2026-10")
    page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=15_000)
    expected_ids = {item["id"] for entries in row["suggestions"].values()
                    for item in entries if item.get("id")}
    page.get_by_role("button", name="保留整份分析").click()
    page.get_by_text("已保留整份分析", exact=False).wait_for(state="visible")
    first_patch = contract.patch_payloads[-1]
    accepted = set(first_patch.get("accepted_suggestion_ids") or [])
    populated = {module.get("field_key"): len(module.get("items") or [])
                 for module in first_patch.get("overview_modules") or []}
    assert expected_ids == accepted, "Whole-analysis accept lost suggestion IDs"
    assert all(populated.get(key, 0) >= len(entries)
               for key, entries in row["suggestions"].items()), "Module fill lost result rows"

    first_area = page.locator(".pa-overview-module textarea").first
    first_area.fill("隔离测试人工修订")
    title = page.get_by_role("textbox", name="新模块名称")
    title.fill("自定义测试模块")
    page.get_by_role("button", name="新增模块").click()
    custom = page.locator(".pa-overview-module").last
    custom.locator(".pa-add-item").click()
    custom.locator("textarea").fill("新增的细分备注")
    titles = page.locator(".pa-overview-module input[aria-label='模块标题']").evaluate_all(
        "(nodes) => nodes.map(node => node.value)")
    assert "季节性" in titles, "Seasonality card disappeared before edit"
    removable = page.locator(".pa-overview-module").nth(titles.index("季节性"))
    assert removable.get_by_role("button", name="删除此条内容").count() > 0
    removable.get_by_role("button", name="删除此条内容").first.click()
    page.get_by_role("button", name="保存概况").click()
    page.get_by_text("档案概况已保存", exact=True).wait_for(state="visible")
    second_patch = contract.patch_payloads[-1]
    modules = second_patch.get("overview_modules") or []
    assert any(item.get("text") == "隔离测试人工修订"
               for module in modules for item in module.get("items") or []), "Edit was not saved"
    assert any(module.get("title") == "自定义测试模块" and
               any(item.get("text") == "新增的细分备注"
                   for item in module.get("items") or []) for module in modules), "New card was not saved"
    page.reload(wait_until="domcontentloaded")
    page.locator("main.pa-dossier h1").wait_for(state="visible")
    values = page.locator(".pa-overview-module textarea").evaluate_all("(nodes) => nodes.map(node => node.value)")
    assert "隔离测试人工修订" in values, "Saved edit vanished after reload"
    assert "新增的细分备注" in values, "Added card vanished after reload"
    assert not contract.blocked_unknown_api and contract.page_errors == 0, (
        contract.blocked_unknown_api, contract.page_errors)
    context.close()
    return {"accepted_ids": len(accepted), "populated_modules": populated,
            "saved_module_count": len(modules), "patches": len(contract.patch_payloads)}


def drive_ignore(browser, base_url: str, row: dict) -> dict:
    contract = contract_with_real_result(base_url, row, baseline=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = base.open_research_page(context, base_url, contract)
    base.start_run(page, category="男士休闲裤", month="2026-10")
    page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=15_000)
    page.get_by_role("button", name="忽略整份分析").click()
    page.get_by_text("已忽略整份分析", exact=False).wait_for(state="visible")
    patch = contract.patch_payloads[-1]
    ids = {item["id"] for entries in row["suggestions"].values()
           for item in entries if item.get("id")}
    assert set(patch.get("ignored_suggestion_ids") or []) == ids
    modules = patch.get("overview_modules") or []
    assert len(modules) == 1 and [item["text"] for item in modules[0]["items"]] == [
        "隔离档案原有内容"], "Ignore changed existing archive content"
    page.reload(wait_until="domcontentloaded")
    page.locator("main.pa-dossier h1").wait_for(state="visible")
    values = page.locator(".pa-overview-module textarea").evaluate_all("(nodes) => nodes.map(node => node.value)")
    assert "隔离档案原有内容" in values
    assert not contract.blocked_unknown_api and contract.page_errors == 0, (
        contract.blocked_unknown_api, contract.page_errors)
    context.close()
    return {"ignored_ids": len(ids), "original_item_preserved": True}


def drive_recovery_and_conflict(browser, base_url: str, row: dict) -> dict:
    contract = contract_with_real_result(base_url, row)
    original_expected_run = contract.expected_run
    original_handle = contract.handle
    state = {"conflict": False, "recovery_calls": 0}

    def expected_run(status: str, phase: str, **kwargs):
        if status == "ready":
            return original_expected_run(status, phase, **kwargs)
        if status == "failed":
            failed = original_expected_run(status, phase, **kwargs)
            failed["recovery"] = {"available": True, "reason": "保存的原始结果可离线恢复"}
            failed["usage"] = row.get("usage") or {}
            failed["error"] = "模型结果的字段边界未通过结构解析"
            failed["phase"] = "validating"
            return failed
        return original_expected_run(status, phase, **kwargs)

    def handle(route):
        path = route.request.url.split("?", 1)[0]
        if route.request.method == "POST" and path.endswith(f"/overview-drafts/{base.RUN_ID}/recover"):
            state["recovery_calls"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=base.response_body(expected_run("ready", "ready")))
            return
        if state["conflict"] and route.request.method == "PATCH" and path.endswith(
                f"/api/product-archive/archives/{base.ARCHIVE_ID}"):
            route.fulfill(status=409, content_type="application/json",
                          body=base.response_body({"detail": "synthetic version conflict"}))
            return
        original_handle(route)

    contract.expected_run = expected_run
    contract.handle = handle
    contract.scenario = "failure"
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = base.open_research_page(context, base_url, contract)
    base.start_run(page, category="男士休闲裤", month="2026-10")
    page.locator(".overview-failed").wait_for(state="visible", timeout=15_000)
    failed_text = page.locator(".overview-failed").inner_text()
    assert "模型报告输入" in failed_text and "已报告费用" in failed_text
    page.get_by_role("button", name="离线恢复分析结果").click()
    page.wait_for_timeout(600)
    if not page.locator(".overview-suggestion-preview").count():
        print(json.dumps({"recovery_calls": state["recovery_calls"],
                          "unknown_api": contract.blocked_unknown_api,
                          "alerts": page.locator("[role='alert']").all_text_contents()},
                         ensure_ascii=False), flush=True)
    page.locator(".overview-suggestion-preview").wait_for(state="visible")
    assert state["recovery_calls"] == 1, "Recovery endpoint called more than once"
    page.get_by_role("button", name="保留整份分析").click()
    page.get_by_text("已保留整份分析", exact=False).wait_for(state="visible")
    state["conflict"] = True
    page.locator(".pa-overview-module textarea").first.fill("冲突修改不应保存")
    page.get_by_role("button", name="保存概况").click()
    page.locator("[role='alert']").filter(has_text="保存冲突").wait_for(state="visible")
    assert len(contract.patch_payloads) == 1, "Conflict path unexpectedly persisted a second patch"
    context.close()
    return {"offline_recovery_calls": state["recovery_calls"],
            "failed_usage_visible": True, "save_conflict_visible": True}


def drive_direct_visit(browser, base_url: str, row: dict) -> dict:
    contract = contract_with_real_result(base_url, row)
    original_handle = contract.handle

    def handle(route):
        path = route.request.url.split("?", 1)[0]
        if route.request.method == "GET" and path.endswith(
                f"/api/product-archive/archives/{base.ARCHIVE_ID}/overview-drafts/latest-open"):
            route.fulfill(status=200, content_type="application/json",
                          body=base.response_body({"id": base.RUN_ID, "status": "ready"}))
            return
        original_handle(route)

    contract.handle = handle
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = base.open_research_page(context, base_url, contract)
    page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=15_000)
    count = page.locator(".overview-preview-item").count()
    assert count == sum(len(entries) for entries in row["suggestions"].values())
    assert not contract.create_payloads, "Direct visit unexpectedly started a model task"
    assert not contract.blocked_unknown_api and contract.page_errors == 0
    context.close()
    return {"preview_items": count, "generation_requests": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-url", default="http://127.0.0.1:5174")
    parser.add_argument("--api-url", default="http://127.0.0.1:8896")
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    row = read_real_run(args.api_url, args.archive_id, args.run_id)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            accepted = drive_accept(browser, args.page_url, row)
            ignored = drive_ignore(browser, args.page_url, row)
            recovery = drive_recovery_and_conflict(browser, args.page_url, row)
            direct = drive_direct_visit(browser, args.page_url, row)
        finally:
            browser.close()
    print(json.dumps({"run_id": args.run_id, "isolated_browser_accept": accepted,
                      "isolated_browser_ignore": ignored,
                      "isolated_browser_recovery": recovery,
                      "isolated_browser_direct_visit": direct}, ensure_ascii=False))


if __name__ == "__main__":
    main()
