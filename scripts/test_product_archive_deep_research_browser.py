"""Browser acceptance for Product Archive deep research using synthetic APIs.

Only the DEV frontend shell is loaded from the configured loopback origin.
Every Product Archive API call is fulfilled by this script; unknown API calls
are rejected, and all non-loopback browser requests are aborted. No model,
MCP, Amazon, keyword, or review job can be started by this test.

The preflight and per-question fields below are forward-looking UI contracts.
This script deliberately reports missing UI wiring as failed checks instead of
changing product code or inventing a pass from source-text assertions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ARCHIVE_ID = "synthetic-deep-research-archive"
RUN_ID = "synthetic-deep-research-run"
TIMEOUT_MS = 10_000
POLL_INTERVAL_MS = 1_200


def local_http_origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if not parsed.port:
        return None
    return parsed.scheme, parsed.hostname, parsed.port


def response_body(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def fixture_archive() -> dict:
    return {
        "id": ARCHIVE_ID,
        "title": "Synthetic market research",
        "category": "Synthetic outdoor swim shirt",
        "marketplace": "US",
        "data_month": "2026-09",
        "workspace_id": None,
        "mapping_confirmed": True,
        "dataset": {"name": "Synthetic dataset", "count": 12},
        "mapping": {},
        "overview": {},
        "overview_suggestions": {},
        "metrics": {"sample_rows": 12},
        "integrations": {},
    }


def fixture_sources() -> list[dict]:
    return [
        {"id": "src-tags", "name": "Synthetic tagging workbook", "type": "tagged_workbook",
         "status": "completed", "date": "2026-09", "available": True, "selected_by_default": True},
        {"id": "src-keywords", "name": "Synthetic keyword results", "type": "keywords",
         "status": "completed", "date": "2026-09", "available": True, "selected_by_default": True},
        {"id": "src-reviews", "name": "Synthetic review results", "type": "reviews",
         "status": "completed", "date": "2026-09", "available": True, "selected_by_default": False},
        {"id": "src-voc", "name": "Synthetic VOC report", "type": "voc",
         "status": "completed", "date": "2026-09", "available": True, "selected_by_default": False},
    ]


def fixture_preflight() -> dict:
    """Expected GET preflight contract for the next research UI iteration."""
    return {
        "research_method": "model_knowledge_first_then_public_verification_then_archive_comparison",
        "model": {"provider": "synthetic-provider", "name": "synthetic-provider/research-model",
                  "reasoning_provider": "synthetic-provider",
                  "reasoning_name": "synthetic-provider/text-reasoner"},
        "budget": {
            "model_requests_max": 3,
            "search_requests_max": 6,
            "unique_pages_max": 12,
            "http_get_requests_max": 24,
            "excerpt_chars_max": 1500,
            "input_chars_max": 48000,
            "output_tokens_max": 16000,
            "images_max": 4,
        },
        "estimate": {"input_tokens": 6200, "image_count": 2,
                     "cost_amount": None, "cost_currency": None, "cost_status": "unknown"},
    }


def fixture_questions() -> list[dict]:
    return [
        {"id": "q-form", "question": "What product forms and segments exist?",
         "status": "completed", "search_attempts": 1, "candidate_count": 3,
         "source_ids": ["web-form"]},
        {"id": "q-needs", "question": "Which jobs, needs, and barriers distinguish buyers?",
         "status": "completed", "search_attempts": 1, "candidate_count": 2,
         "source_ids": ["web-needs"]},
        {"id": "q-season", "question": "Which climate and seasonal patterns are relevant?",
         "status": "partial", "search_attempts": 1, "candidate_count": 1,
         "source_ids": []},
        {"id": "q-alternatives", "question": "What adjacent forms and substitutes should be compared?",
         "status": "completed", "search_attempts": 1, "candidate_count": 2,
         "source_ids": ["web-alternatives"]},
    ]


def fixture_sources_coverage() -> dict:
    return {
        "questions_total": 4,
        "questions_with_sources": 3,
        "search_attempts": 4,
        "search_successes": 4,
        "candidates": 8,
        "pages_read": 3,
        "sources": [
            {"id": "web-form", "title": "Synthetic product forms", "url": "https://example.invalid/forms",
             "question_ids": ["q-form"]},
            {"id": "web-needs", "title": "Synthetic buyer needs", "url": "https://example.invalid/needs",
             "question_ids": ["q-needs"]},
            {"id": "web-alternatives", "title": "Synthetic alternatives", "url": "https://example.invalid/alternatives",
             "question_ids": ["q-alternatives"]},
        ],
    }


def fixture_suggestion() -> dict:
    return {
        "product_function": [{
            "id": "synthetic-function-suggestion",
            "text": "Synthetic analysis: compare sun-protection needs across product forms.",
            "source_type": "overview_draft",
            "segment_id": "S2",
            "segment_name": "Adjacent training wear",
            "segment_classification": "hypothesis",
            "basis": "synthesis",
            "basis_note": "Synthetic public evidence compared with synthetic archive material.",
            "limits": "Synthetic test data only.",
            "evidence_refs": ["web-form", "src-tags"],
            "source_ids": ["src-tags"],
            "links": [{"evidence_id": "web-form", "title": "Synthetic product forms",
                       "url": "https://example.invalid/forms"}],
        }],
    }


class BrowserContract:
    def __init__(self, base_url: str, origin: tuple[str, str, int], scenario: str):
        self.base_url = base_url.rstrip("/")
        self.origin = origin
        self.scenario = scenario
        self.api_requests: list[dict] = []
        self.blocked_external = 0
        self.blocked_unknown_api: list[str] = []
        self.page_errors = 0
        self.create_payloads: list[dict] = []
        self.patch_payloads: list[dict] = []
        self.run_reads = 0
        self.preflight_reads = 0
        self.current_archive = fixture_archive()

    def expected_run(self, status: str, phase: str, *, model_attempted: bool = False,
                     error: str | None = None) -> dict:
        return {
            "id": RUN_ID,
            "archive_id": ARCHIVE_ID,
            "status": status,
            "phase": phase,
            "model_attempted": model_attempted,
            "error": error,
            "model_snapshot": {"provider": "synthetic-provider", "model": "synthetic-provider/research-model",
                               "model_ref": "synthetic-provider/research-model",
                               "reasoning_model_ref": "synthetic-provider/text-reasoner"},
            "usage_estimate": fixture_preflight()["estimate"] | fixture_preflight()["budget"],
            "usage": {"quality": "unknown", "input_tokens": None, "output_tokens": None,
                      "cost_amount": None, "cost_status": "unknown"},
            "public_research_status": "partial",
            "public_research_diagnostics": {
                "status": "partial", "search_attempts": 6, "search_successes": 5,
                "search_failures": 1, "candidate_count": 8, "page_fetch_successes": 3,
                "page_fetch_failures": 2,
                "message": "Synthetic public research has partial coverage.",
            },
            "research_plan": {
                "mode": "model_plan",
                "questions": [item["question"] for item in fixture_questions()],
                "initial_queries": ["girls swim shirt product forms", "rash guard for girls buyer needs",
                                    "girls swim shirt seasonality", "girls rash guard alternatives"],
                "findings": [{"text": "关键词数据提示泳装型 rash guard 与日常防晒服的搜索意图不同。",
                              "evidence_ids": ["keyword:synthetic-run:term-1"]}],
                "adaptive_questions": ["Which product forms are for pool use versus everyday sun protection?"],
                "adaptive_queries": ["girls swim shirt pool UPF use cases", "girls rash guard seasonal demand"],
            },
            "research_coverage": {
                "first_round_sources": 2, "adaptive_sources": 1,
                "first_round_query_coverage": [
                    {"query": "girls swim shirt product forms", "status": "completed", "source_ids": ["web-form"]},
                    {"query": "rash guard for girls buyer needs", "status": "completed", "source_ids": ["web-needs"]},
                    {"query": "girls swim shirt seasonality", "status": "no_candidates", "source_ids": []},
                    {"query": "girls rash guard alternatives", "status": "no_candidates", "source_ids": []},
                ],
                "adaptive_query_coverage": [
                    {"query": "girls swim shirt pool UPF use cases", "status": "completed", "source_ids": ["web-alternatives"]},
                    {"query": "girls rash guard seasonal demand", "status": "search_failed", "source_ids": [],
                     "failure_code": "search_challenge"},
                ],
            },
            "source_manifest": [
                {"id": "src-tags", "type": "tagged_workbook", "name": "Synthetic tagging workbook", "selected": True},
                {"id": "src-keywords", "type": "keywords", "name": "Synthetic keyword results", "selected": True},
                {"id": "src-reviews", "type": "reviews", "name": "Synthetic review results", "selected": False},
                {"id": "src-voc", "type": "voc", "name": "Synthetic VOC report", "selected": False},
                *fixture_sources_coverage()["sources"],
            ],
            "input_routing": {"text_chars": 10240, "text_chars_max": 48000},
            "citations": fixture_sources_coverage()["sources"],
            "suggestions": fixture_suggestion() if status == "ready" else {},
            "diagnostics": {"segments": [{"id": "S1", "name": "Outdoor sun protection",
                                            "description": "Synthetic core segment.",
                                            "classification": "supported"},
                                           {"id": "S2", "name": "Adjacent training wear",
                                            "description": "Synthetic adjacent hypothesis.",
                                            "classification": "hypothesis"}],
                            "product_forms": [{"id": "F1", "name": "Long sleeve swim top",
                                               "description": "Synthetic product form."}],
                            "needs_validation": [{"segment_id": "S1",
                                                  "text": "Validate whether daily outdoor use is a separate segment."}],
                            "missing_fields": []} if status == "ready" else {},
        }

    def _same_origin(self, url: str) -> bool:
        target = urlparse(url)
        scheme, host, port = self.origin
        return target.scheme == scheme and target.hostname == host and target.port == port

    def handle(self, route) -> None:
        request = route.request
        target = urlparse(request.url)
        if not self._same_origin(request.url):
            self.blocked_external += 1
            route.abort()
            return

        path = target.path
        method = request.method
        if path.startswith("/api/"):
            self.api_requests.append({"method": method, "path": path})
            if method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}":
                route.fulfill(status=200, content_type="application/json", body=response_body(self.current_archive))
                return
            if method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-sources":
                route.fulfill(status=200, content_type="application/json", body=response_body(fixture_sources()))
                return
            if method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts/latest-open":
                route.fulfill(status=200, content_type="application/json",
                              body=response_body({"id": None, "status": None}))
                return
            if method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts/preflight":
                self.preflight_reads += 1
                route.fulfill(status=200, content_type="application/json", body=response_body(fixture_preflight()))
                return
            if method == "POST" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts":
                try:
                    body = json.loads(request.post_data or "{}")
                except ValueError:
                    body = {}
                self.create_payloads.append(body)
                route.fulfill(status=200, content_type="application/json",
                              body=response_body({"id": RUN_ID, "status": "running", "phase": "queued"}))
                return
            if method == "GET" and path == f"/api/product-archive/archives/{ARCHIVE_ID}/overview-drafts/{RUN_ID}":
                self.run_reads += 1
                if self.scenario == "failure":
                    value = self.expected_run("failed", "model_analysis", model_attempted=True,
                                              error="Synthetic model response validation failure.")
                else:
                    sequence = [
                        self.expected_run("running", "preparing_sources"),
                        self.expected_run("running", "public_research"),
                        self.expected_run("running", "holistic_analysis", model_attempted=True),
                        self.expected_run("running", "validating", model_attempted=True),
                        self.expected_run("ready", "ready", model_attempted=True),
                    ]
                    value = sequence[min(self.run_reads - 1, len(sequence) - 1)]
                route.fulfill(status=200, content_type="application/json", body=response_body(value))
                return
            if method == "PATCH" and path == f"/api/product-archive/archives/{ARCHIVE_ID}":
                try:
                    body = json.loads(request.post_data or "{}")
                except ValueError:
                    body = {}
                self.patch_payloads.append(body)
                self.current_archive = self.current_archive | body | {"updated_at": "synthetic-updated"}
                route.fulfill(status=200, content_type="application/json", body=response_body(self.current_archive))
                return
            if method == "GET" and path == f"/api/projects":
                route.fulfill(status=200, content_type="application/json", body="[]")
                return
            if method == "GET" and path == "/api/projects/trash":
                route.fulfill(status=200, content_type="application/json", body="[]")
                return
            if method == "GET" and path == "/api/settings/models":
                route.fulfill(status=200, content_type="application/json",
                              body=response_body({"providers": [], "text_models": [], "default_model": ""}))
                return
            if method == "GET" and path in {"/api/runtime/status", "/api/health"}:
                route.fulfill(status=200, content_type="application/json", body=response_body({"profile": "DEV"}))
                return
            self.blocked_unknown_api.append(f"{method} {path}")
            route.fulfill(status=404, content_type="application/json", body=response_body({"detail": "synthetic_api_not_stubbed"}))
            return

        # The only real network content allowed is the static application shell
        # and assets served from this exact loopback origin.
        route.continue_()


def check(summary: dict, name: str, passed: bool, failure_reason: str) -> None:
    summary["checks"][name] = bool(passed)
    if not passed:
        summary["contract_gaps"].append({"check": name, "reason": failure_reason})


def open_research_page(context, base_url: str, contract: BrowserContract):
    context.route("**/*", contract.handle)
    context.add_init_script("""
      (() => {
        const NativeWebSocket = window.WebSocket;
        function GuardedWebSocket(url, protocols) {
          const target = new URL(url, window.location.href);
          const loopback = ["127.0.0.1", "localhost", "::1"].includes(target.hostname)
            && target.port === window.location.port;
          if (!loopback) {
            window.__blockedExternalWebSockets = (window.__blockedExternalWebSockets || 0) + 1;
            throw new DOMException("Non-loopback WebSocket blocked by browser acceptance", "SecurityError");
          }
          return protocols === undefined ? new NativeWebSocket(url) : new NativeWebSocket(url, protocols);
        }
        GuardedWebSocket.prototype = NativeWebSocket.prototype;
        Object.assign(GuardedWebSocket, NativeWebSocket);
        window.WebSocket = GuardedWebSocket;
      })();
    """)
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    page.set_default_navigation_timeout(TIMEOUT_MS)
    page.on("pageerror", lambda _error: setattr(contract, "page_errors", contract.page_errors + 1))
    page.goto(f"{base_url.rstrip('/')}/#/product-archive/{ARCHIVE_ID}", wait_until="domcontentloaded")
    page.locator("main.pa-dossier h1").wait_for(state="visible", timeout=TIMEOUT_MS)
    return page


def collect_browser_safety(summary: dict, contract: BrowserContract, page) -> None:
    try:
        blocked_sockets = int(page.evaluate("window.__blockedExternalWebSockets || 0"))
    except Exception:
        blocked_sockets = 0
    summary["browser_safety"]["blocked_external_requests"] += contract.blocked_external + blocked_sockets
    summary["browser_safety"]["unknown_api_calls"] += contract.blocked_unknown_api
    summary["browser_safety"]["page_errors"] += contract.page_errors


def inspect_preflight(page, summary: dict) -> None:
    panel = page.locator(".pa-overview-generate")
    page.locator(".pa-research-preflight").get_by_text("synthetic-provider/research-model", exact=False).wait_for(
        state="visible", timeout=TIMEOUT_MS,
    )
    text = panel.inner_text(timeout=TIMEOUT_MS)
    check(summary, "preflight_budget_caps", all(item in text for item in (
        "6 次搜索", "12 篇网页", "HTTP 请求 24 次", "48,000", "16,000", "模型请求 3 次",
    )), "preflight budget caps not visible")
    check(summary, "model_knowledge_primary_explanation",
          bool(re.search(r"模型(?:知识|通识).{0,40}(?:先|优先)|(?:先|优先).{0,40}模型(?:知识|通识)", text)),
          "preflight does not explain model knowledge first, followed by web verification and archive comparison")
    check(summary, "preflight_actual_estimate_and_model",
          "synthetic-provider/research-model" in text and "6200" in text and "2 张" in text,
          "preflight does not show the configured model, estimated input tokens, and image count")
    check(summary, "reasoning_and_vision_models_distinguished",
          "研究规划与补查模型" in text and "synthetic-provider/text-reasoner" in text,
          "preflight does not distinguish the text reasoning model from the image synthesis model")
    check(summary, "preflight_unknown_cost", bool(re.search(r"费用.{0,8}未知|金额.{0,8}未知", text)),
          "preflight does not show unknown cost when price is unavailable")
def start_run(page, category: str = "Synthetic outdoor swim shirt", month: str = "2026-09") -> None:
    print("[deep-research-browser] fill synthetic research inputs", flush=True)
    panel = page.locator(".pa-overview-generate")
    panel.locator("label").filter(has_text="品类/测试主题").locator("input").fill(category)
    panel.locator("label").filter(has_text="数据月份").locator("input").fill(month)
    print("[deep-research-browser] submit synthetic research request", flush=True)
    page.get_by_role("button", name="生成档案建议").click()


def inspect_progress_and_coverage(page, contract: BrowserContract, summary: dict) -> None:
    start_run(page)
    stage_label = page.locator(".overview-progress-stage b").filter(has_text="分析阶段：").first
    stage_label.wait_for(state="visible", timeout=TIMEOUT_MS)
    try:
        page.locator(".overview-progress-stage b").filter(has_text="整理已选档案资料").first.wait_for(
            state="visible", timeout=TIMEOUT_MS,
        )
        check(summary, "progress_preparing_sources", True, "")
    except PlaywrightTimeoutError:
        check(summary, "progress_preparing_sources", False, "preparing_sources phase not rendered")

    page.locator(".overview-progress-stage b").filter(has_text="首轮公开资料检索").first.wait_for(
        state="visible", timeout=TIMEOUT_MS,
    )
    check(summary, "progress_public_research", True, "")
    question_text = page.get_by_text("What product forms and segments exist?", exact=True)
    check(summary, "research_questions_visible", question_text.count() > 0 and question_text.first.is_visible(),
          "per-question research plan/result was not rendered")
    check(summary, "query_level_coverage_visible",
          page.get_by_text("已找到资料 · 命中 1 篇", exact=False).count() > 0,
          "individual search query did not expose its source count/status")
    check(summary, "source_coverage_visible",
          page.get_by_text("首轮 2 篇 · 补查 1 篇", exact=False).count() > 0
          and page.get_by_text("首轮 2 篇 · 补查 1 篇", exact=False).first.is_visible(),
          "source coverage by research round was not rendered")
    check(summary, "model_name_visible_during_run",
          page.get_by_text("synthetic-provider/research-model", exact=False).count() > 0,
          "active run model identity is not visible")
    check(summary, "archive_material_is_comparison_only",
          page.get_by_text("档案资料校正和补充", exact=False).count() > 0,
          "run progress does not distinguish archive comparison from model knowledge and public research")

    page.locator(".overview-progress-stage b").filter(has_text="统一综合分析").first.wait_for(
        state="visible", timeout=TIMEOUT_MS,
    )
    check(summary, "holistic_phase_visible", True, "")
    page.locator(".overview-progress-stage b").filter(has_text="校验分析结构与来源").first.wait_for(
        state="visible", timeout=TIMEOUT_MS,
    )
    check(summary, "validation_phase_visible", True, "")
    page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=TIMEOUT_MS)
    print("[deep-research-browser] ready preview rendered", flush=True)
    research_summary = page.locator("details.pa-research-summary")
    check(summary, "research_coverage_open_for_result_review",
          research_summary.count() > 0 and research_summary.first.evaluate("node => node.open"),
          "research questions and coverage were not expanded when the result became ready")
    check(summary, "research_questions_remain_visible_for_result_review",
          page.get_by_text("What product forms and segments exist?", exact=True).is_visible(),
          "research questions were not visible alongside the ready result")
    structure_summary = page.locator("details.overview-structure-summary")
    check(summary, "result_structure_summary_available",
          structure_summary.count() > 0 and "2 个细分" in structure_summary.first.inner_text()
          and "1 项待验证" in structure_summary.first.inner_text(),
          "result did not expose its recognized segments, product forms, and validation count")
    if structure_summary.count() > 0:
        structure_summary.locator("summary").click()
        check(summary, "supported_and_hypothesis_segments_distinguished",
              "档案中有线索" in structure_summary.inner_text()
              and "模型推断 · 待验证" in structure_summary.inner_text()
              and "不代表细分人群规模或需求已被验证" in structure_summary.inner_text(),
              "result did not distinguish archive clues from unverified market conclusions")
        check(summary, "result_validation_hypotheses_visible",
              "Validate whether daily outdoor use is a separate segment." in structure_summary.inner_text(),
              "model validation hypotheses were not visible in the expandable result summary")
    check(summary, "claim_support_tier_next_to_conclusion",
          page.get_by_text("模型推断 · 待验证", exact=True).count() >= 2,
          "a hypothesis conclusion did not show its segment support tier beside the claim")
    check(summary, "partial_research_failures_visible_at_preview",
          page.get_by_text("网页读取失败 2 次", exact=False).count() > 0,
          "partial web research failure count was hidden from the result decision point")
    check(summary, "search_challenge_explained_in_plain_language",
          page.get_by_text("搜索服务暂时限制", exact=False).count() > 0,
          "search challenge code was not translated to a readable status")
    check(summary, "pending_suggestion_names_archive_source",
          page.get_by_text("对照资料：Synthetic tagging workbook", exact=True).count() > 0,
          "pending suggestion did not show a human-readable archive source name")
    if research_summary.count() > 0:
        check(summary, "first_round_finding_shows_keyword_evidence",
              page.get_by_text("关键词数据提示泳装型 rash guard 与日常防晒服的搜索意图不同。", exact=True).count() > 0
              and page.get_by_text("依据：Synthetic keyword results", exact=True).count() > 0,
              "first-round evidence-backed finding or its keyword-task source was not shown")
        research_summary.locator("summary").click()
        check(summary, "research_coverage_can_be_collapsed",
              not research_summary.first.evaluate("node => node.open"),
              "user could not collapse the research coverage details")
        research_summary.locator("summary").click()
        check(summary, "research_coverage_can_be_reopened",
              research_summary.first.evaluate("node => node.open"),
              "user could not reopen the research coverage details")
    source_link = page.get_by_role("link", name="Synthetic product forms")
    check(summary, "source_coverage_citations_visible",
          source_link.count() > 0 and source_link.first.is_visible(),
          "ready preview did not expose a source link")


def inspect_failure_refresh(browser, base_url: str, summary: dict) -> None:
    contract = BrowserContract(base_url, local_http_origin(base_url), "failure")
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = open_research_page(context, base_url, contract)
    start_run(page)
    page.get_by_text("分析未完成：", exact=False).wait_for(state="visible", timeout=TIMEOUT_MS)
    failure_text = page.locator(".overview-failed").inner_text()
    check(summary, "failure_phase_and_model_attempt", "模型分析失败" in failure_text
          and "模型请求已发出" in failure_text,
          "failure view omits failure phase or whether a model request was sent")
    check(summary, "failure_cost_unknown", "费用未知" in failure_text,
          "failure view does not preserve unknown-cost state")
    page.reload(wait_until="domcontentloaded")
    page.locator("main.pa-dossier h1").wait_for(state="visible", timeout=TIMEOUT_MS)
    try:
        page.get_by_text("分析未完成：", exact=False).wait_for(state="visible", timeout=2_500)
        restored = True
    except PlaywrightTimeoutError:
        restored = False
    check(summary, "failed_run_survives_refresh", restored,
          "failed run and diagnostics disappeared after refresh")
    collect_browser_safety(summary, contract, page)
    context.close()


def inspect_decision_and_edit(browser, base_url: str, decision: str, summary: dict) -> None:
    contract = BrowserContract(base_url, local_http_origin(base_url), "ready")
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = open_research_page(context, base_url, contract)
    start_run(page)
    page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=TIMEOUT_MS)
    if decision == "accepted":
        page.get_by_role("button", name="保留整份分析").click()
        page.get_by_text("已保留整份分析", exact=False).wait_for(state="visible", timeout=TIMEOUT_MS)
        field = "whole_analysis_keep"
        expected_key = "accepted_suggestion_ids"
    else:
        page.get_by_role("button", name="忽略整份分析").click()
        page.get_by_text("已忽略整份分析", exact=False).wait_for(state="visible", timeout=TIMEOUT_MS)
        field = "whole_analysis_ignore"
        expected_key = "ignored_suggestion_ids"
    check(summary, field, bool(contract.patch_payloads)
          and bool(contract.patch_payloads[-1].get(expected_key)),
          f"{decision} action did not persist the corresponding suggestion IDs")

    textareas = page.locator(".pa-overview-module textarea")
    if textareas.count() == 0:
        empty_card = page.locator(".pa-overview-module").first
        add_button = empty_card.locator(".pa-add-item") if empty_card.count() else page.locator(".pa-add-item").first
        if add_button.count():
            add_button.click()
            textareas = page.locator(".pa-overview-module textarea")
    if textareas.count() == 0:
        cards = page.locator(".pa-overview-module").count()
        add_buttons = page.locator(".pa-add-item").count()
        check(summary, "editable_module_after_decision", False,
              f"editable module/textarea missing after decision (cards={cards}, add_buttons={add_buttons})")
    else:
        check(summary, "editable_module_after_decision", True, "")
        before = [textareas.nth(index).input_value() for index in range(textareas.count())]
        if decision == "accepted":
            suggestion_present = any("Synthetic analysis:" in value for value in before)
            check(summary, "kept_suggestion_in_module", suggestion_present,
                  "accepted analysis was not added to an editable module")
        else:
            check(summary, "ignored_suggestion_not_added", not any("Synthetic analysis:" in value for value in before),
                  "ignored analysis changed existing module content")
        textareas.first.fill("Synthetic manual edit after whole-analysis decision.")
        page.get_by_role("button", name="保存概况").click()
        page.get_by_text("档案概况已保存", exact=True).wait_for(state="visible", timeout=TIMEOUT_MS)
        check(summary, "module_edit_saved", bool(contract.patch_payloads)
              and "Synthetic manual edit after whole-analysis decision." in json.dumps(contract.patch_payloads[-1]),
              "manual module edit was not included in the archive save")

    collect_browser_safety(summary, contract, page)
    context.close()


def inspect_responsive(browser, base_url: str, summary: dict) -> None:
    for width, height in ((360, 780), (390, 844)):
        contract = BrowserContract(base_url, local_http_origin(base_url), "ready")
        context = browser.new_context(viewport={"width": width, "height": height}, service_workers="block")
        page = open_research_page(context, base_url, contract)
        start_run(page)
        page.locator(".overview-suggestion-preview").wait_for(state="visible", timeout=TIMEOUT_MS)
        measured = page.evaluate("({viewport: window.innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth})")
        passed = measured["document"] <= measured["viewport"] and measured["body"] <= measured["viewport"]
        check(summary, f"responsive_no_horizontal_overflow_{width}px", passed,
              f"horizontal overflow at {width}px: viewport={measured['viewport']} document={measured['document']} body={measured['body']}")
        collect_browser_safety(summary, contract, page)
        context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5174",
                        help="loopback DEV frontend only; all API responses are synthetic")
    parser.add_argument("--output", default="", help="optional sanitized JSON result path")
    parser.add_argument("--screenshot", default="", help="optional screenshot of the synthetic ready result preview")
    args = parser.parse_args()
    origin = local_http_origin(args.base_url)
    if origin is None:
        print(json.dumps({"status": "blocked", "reason": "base_url_must_be_loopback_http_with_port"}))
        return 2

    summary = {
        "status": "failed",
        "base_origin": f"{origin[0]}://{origin[1]}:{origin[2]}",
        "fixture": "synthetic_product_archive_and_deep_research",
        "api_mode": "page_route_fulfills_archive_sources_preflight_draft_and_save; unknown_api_rejected",
        "browser_safety": {"external_requests_blocked": True, "blocked_external_requests": 0,
                           "unknown_api_calls": [], "page_errors": 0},
        "checks": {},
        "contract_gaps": [],
        "not_run": [],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    stage = "browser_startup"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                print("[deep-research-browser] open synthetic archive", flush=True)
                context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
                contract = BrowserContract(args.base_url, origin, "ready")
                page = open_research_page(context, args.base_url, contract)

                stage = "preflight_contract"
                print(f"[deep-research-browser] {stage}", flush=True)
                inspect_preflight(page, summary)
                check(summary, "preflight_api_contract_consumed", contract.preflight_reads > 0,
                      "the UI did not request the expected preflight API contract")

                stage = "research_progress_and_coverage"
                print(f"[deep-research-browser] {stage}", flush=True)
                try:
                    inspect_progress_and_coverage(page, contract, summary)
                except PlaywrightTimeoutError:
                    summary["not_run"].append("progress_and_coverage: timed out before expected research flow completed")
                    check(summary, "research_progress_flow", False,
                          "research flow did not render the expected phases and ready result")
                except Exception as exc:
                    print(f"[deep-research-browser] progress check detail: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
                    summary["not_run"].append(f"progress_and_coverage: runtime_{type(exc).__name__}")
                    check(summary, "research_progress_flow", False, "research progress flow could not complete")

                if args.screenshot and page.locator(".overview-suggestion-preview").count():
                    from pathlib import Path
                    screenshot = Path(args.screenshot)
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.locator(".overview-suggestion-preview").first.screenshot(path=str(screenshot))
                    summary["synthetic_preview_screenshot_saved"] = True

                collect_browser_safety(summary, contract, page)
                context.close()

                stage = "failure_refresh"
                print(f"[deep-research-browser] {stage}", flush=True)
                inspect_failure_refresh(browser, args.base_url, summary)
                stage = "decision_keep"
                print(f"[deep-research-browser] {stage}", flush=True)
                inspect_decision_and_edit(browser, args.base_url, "accepted", summary)
                stage = "decision_ignore"
                print(f"[deep-research-browser] {stage}", flush=True)
                inspect_decision_and_edit(browser, args.base_url, "ignored", summary)
                stage = "responsive"
                print(f"[deep-research-browser] {stage}", flush=True)
                inspect_responsive(browser, args.base_url, summary)
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        summary["not_run"].append(f"timeout_at_{stage}")
        summary["contract_gaps"].append({"check": stage, "reason": "browser timed out"})
    except Exception as exc:
        # Keep diagnostics sanitized; exception messages can contain page/API data.
                print(f"[deep-research-browser] failed at {stage}: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
                summary["not_run"].append(f"runtime_{type(exc).__name__}_at_{stage}")
                summary["contract_gaps"].append({"check": stage, "reason": f"runtime_{type(exc).__name__}"})

    if summary["browser_safety"]["blocked_external_requests"]:
        summary["contract_gaps"].append({"check": "network_isolation", "reason": "browser attempted non-loopback request; it was blocked"})
    if summary["browser_safety"]["unknown_api_calls"]:
        summary["contract_gaps"].append({"check": "api_fixture_coverage", "reason": "one or more API paths were not stubbed and returned a synthetic 404"})
    if summary["browser_safety"]["page_errors"]:
        summary["contract_gaps"].append({"check": "browser_runtime", "reason": "one or more page errors occurred"})
    summary["status"] = "passed" if not summary["contract_gaps"] else "failed"

    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    if args.output:
        from pathlib import Path
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized + "\n")
    print(serialized)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
