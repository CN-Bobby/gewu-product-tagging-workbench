"""Reasonix-backed Product Archive draft execution and normalisation."""
from __future__ import annotations

import json
import time

from . import image_observation, overview_drafts as o, reasonix_research, service


def _record_cli_usage(row: dict, envelope: dict, *, image_count: int = 0) -> dict:
    """Keep the CLI's measured aggregate even if its business JSON is invalid."""
    from ..usage import normalize_provider_usage

    raw = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    normalized = normalize_provider_usage(raw)
    input_tokens, output_tokens = normalized.get("input_tokens"), normalized.get("output_tokens")
    cost, currency = envelope.get("total_cost"), envelope.get("currency")
    measured_cost = (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                     and cost > 0 and currency in {"USD", "CNY"})
    row["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens if isinstance(input_tokens, int)
                    and isinstance(output_tokens, int) else None,
                    "cache_read_input_tokens": normalized.get("cache_hit_tokens"),
                    "cache_creation_input_tokens": normalized.get("cache_creation_tokens"),
                    "quality": "measured" if input_tokens is not None or output_tokens is not None
                    else "unknown", "image_count": image_count,
                    "cost_amount": cost if measured_cost else None,
                    "cost_currency": currency if measured_cost else None,
                    "cost_status": "measured" if measured_cost else "unknown"}
    row["raw_result_available"] = isinstance(envelope.get("result"), str) and bool(envelope["result"])
    return raw


def reconcile_recorded_usage(run_id: str, envelope: dict) -> None:
    """Fill the existing failed agent event from its saved CLI envelope."""
    from sqlalchemy import select
    from .. import usage
    from ..db import Session, UsageEvent

    if not isinstance(envelope.get("usage"), dict) or not envelope["usage"]:
        return
    with Session() as session:
        event = session.scalar(select(UsageEvent).where(
            UsageEvent.module == "product_archive", UsageEvent.kind == "agent_turn",
            UsageEvent.task_id == run_id,
            UsageEvent.instance_id == usage.instance_identity()).order_by(
                UsageEvent.occurred_at.desc()))
        event_id = event.id if event else None
        status = event.status if event else None
    if event_id and status in {"failed", "completed", "success"}:
        usage.finish_usage_event(event_id, status=status, usage=envelope["usage"],
                                 usage_source="agent_reported_aggregate")


def apply_research_coverage(row: dict, result: dict) -> None:
    """Use the same visible research record for live and offline-normalized runs."""
    citations = result["citations"]
    searches = [item for item in result["ledger"] if item.get("kind") == "search"]
    fetches = [item for item in result["ledger"] if item.get("kind") == "fetch"]
    queries = list(dict.fromkeys(str(item.get("query")) for item in searches if item.get("query")))
    successful_queries = {query for query in queries if any(
        item.get("query") == query and item.get("status") in {"success", "empty"}
        for item in searches)}
    search_failures = len(queries) - len(successful_queries)
    fetch_failures = sum(item.get("status") != "success" for item in fetches)
    fetched_ids = {item["evidence_id"] for item in citations}
    source_ids_by_query = {query: list(dict.fromkeys(
        str(item["evidence_id"]) for item in fetches
        if item.get("query") == query and item.get("status") == "success"
        and item.get("evidence_id") in fetched_ids))
        for query in queries}
    candidate_queries = {item.get("query") for item in searches if item.get("status") == "success"}
    row["research_plan"] = {"mode": "reasonix_skill", "questions": result["questions"],
                            "initial_queries": queries, "findings": result["findings"],
                            "adaptive_questions": [], "adaptive_queries": []}
    row["research_coverage"] = {"questions": result["questions"], "first_round_queries": queries,
        "first_round_query_coverage": [{"query": query,
            "status": "completed" if source_ids_by_query[query]
                      else "candidates_found" if query in candidate_queries
                      else "search_failed" if query not in successful_queries
                      else "no_candidates",
            "source_ids": source_ids_by_query[query]} for query in queries],
        "first_round_sources": len(citations), "adaptive_questions": [],
        "adaptive_queries": [], "adaptive_query_coverage": [], "adaptive_sources": 0}
    row["public_research_status"] = "partial" if citations and (search_failures or fetch_failures) else (
        "completed" if citations else "unavailable")
    row["public_research_diagnostics"] = {"status": row["public_research_status"],
        "engine": "reasonix_cli_skill", "search_attempts": len(queries),
        "search_successes": len(successful_queries), "search_failures": search_failures,
        "page_fetch_attempts": len(fetches),
        "page_fetch_successes": sum(item.get("status") == "success" for item in fetches),
        "page_fetch_failures": fetch_failures, "search_request_limit": 6, "page_fetch_limit": 8}


def run(archive_id: str, run_id: str, row: dict) -> None:
    from ..usage import start_usage_event, finish_usage_event, estimate_text_tokens

    event_id = None
    started = None
    try:
        o._set_phase(archive_id, run_id, "preparing_sources")
        archive = service._read(archive_id)
        # Older saved jobs may predate the explicit scope/date snapshot fields.
        # Keep them runnable while treating archive month as descriptive context only.
        if not str(row.get("category_context") or "").strip():
            row["category_context"] = str(archive.get("category") or "").strip()[:300]
        row.setdefault("research_scope", {"category_context": row.get("category_context") or
                                          str(archive.get("category") or "").strip()[:300]})
        row.setdefault("data_month_context", str(archive.get("data_month") or "").strip()[:20])
        manifest, materials, issues = o._selected_sources(archive, row.get("selected_source_ids") or [])
        snapshot = row.get("model_snapshot") or {}
        if not snapshot.get("available", True):
            raise ValueError("当前研究模型授权不可用")
        keyword_runs = {item["task_run_id"] for item in materials
                        if item["type"] == "keywords" and item.get("task_run_id")}
        keyword_material = o._completed_keyword_material(archive, keyword_runs)
        keyword_source_months = {str(item.get("run_id")): str(item.get("data_month") or "")
            for item in (((archive.get("integrations") or {}).get("keyword_research") or {}).get("sources") or [])
            if isinstance(item, dict) and item.get("run_id")}
        sample = (o._compact_sample(archive, include_tag_features=False, include_keywords=False)
                  if any(item["type"] == "tagged_workbook" for item in materials)
                  else {"note": "用户未选择当前档案打标表"})
        sample.pop("data_month", None)
        sample["keyword_evidence"] = keyword_material["keyword_evidence"]
        from . import keyword_seasonality
        keyword_trends = keyword_seasonality.from_selected_materials(archive, materials)
        months = sorted(set(o._comparable_demand_months(archive, keyword_runs)) |
                        set(keyword_trends["months"] if keyword_trends else []))
        evidence, refs = {}, []
        for material in materials:
            eid = "local_sample:aggregate" if material["type"] == "sales_sample" else material["source_id"]
            source_type = {"sales_sample": "local_sample", "reviews": "amazon_reviews",
                           "voc": "amazon_voc"}.get(material["type"], material["type"])
            evidence[eid] = {"source_type": source_type, "source_id": material["source_id"]}
            if keyword_trends and eid == keyword_trends["source_id"]:
                evidence[eid].update(data_months=keyword_trends["months"], metric="monthly_searches")
            refs.append({"id": eid, "type": source_type})
        for item in sample.get("keyword_evidence") or []:
            run_id_ref = str(item["id"]).split(":", 2)[1] if str(item["id"]).count(":") >= 2 else ""
            evidence[item["id"]] = {"source_type": "keyword_research", "source_id": item["id"],
                                    "data_month": keyword_source_months.get(run_id_ref)}
            refs.append({"id": item["id"], "type": "keyword_research"})
        compacted, remaining = [], o.MAX_ANALYSIS_TEXT_CHARS
        for material in materials:
            if material["type"] == "sales_sample" or remaining <= 0:
                continue
            encoded = json.dumps(material["content"], ensure_ascii=False)
            chunk = encoded[:min(12000, remaining)]
            truncated = len(chunk) < len(encoded)
            compacted.append({"source_id": material["source_id"], "type": material["type"],
                              "content": {"excerpt": chunk, "truncated": True} if truncated else
                              material["content"], "truncated": truncated})
            remaining -= len(chunk)
        brief = {"category_context": row["category_context"], "marketplace": archive.get("marketplace"),
                 "research_scope": row.get("research_scope") or {"category_context": row["category_context"]},
                 "date_context": {"archive_month_background": archive.get("data_month"),
                                  "legacy_month_background": row.get("data_month_context") or None,
                                  "seasonality_evidence": "档案月份仅作背景，不可作为季节性证据；季节性需求判断须引用本次所选资料中的可比多月序列。"},
                 "sample_summary": sample,
                 "materials": compacted, "evidence_refs": refs,
                 "comparable_demand_months": months, "sources": [],
                 "keyword_boundary": "关键词只能引用已完成关键词任务中的原词与数据；搜索需求季节性必须依据该报告内可比的逐月序列，不能由档案月份推断。"}
        if keyword_trends:
            brief["keyword_monthly_trends"] = {
                "source_id": keyword_trends["source_id"], "metric": "monthly_searches",
                "months": keyword_trends["months"], "keywords": keyword_trends["keywords"],
                "note": "这是已选关键词报告中的逐月搜索需求序列，可支持搜索需求高点；不代表商品销量，不同词可能对应不同细分。"}
        image_paths = [path for item in materials for path in item.get("image_paths", [])]
        tagged_source_id = next((item["source_id"] for item in materials
                                 if item["type"] == "tagged_workbook"), None)
        use_images = bool(snapshot.get("uses_vision"))
        if image_paths and not use_images:
            issues.append({"source_id": tagged_source_id,
                           "reason": "当前模型不支持视觉输入，关联图片未纳入本次分析"})
        vision_result = {"observations": [], "image_count": 0, "usage": {}, "issue": ""}
        if image_paths and use_images:
            o._set_phase(archive_id, run_id, "image_observation")
            row["model_attempted"] = True
            row["phase"] = "image_observation"
            o._save(archive_id, run_id, row)
            vision_result = image_observation.observe(image_paths[:o.MAX_ANALYSIS_IMAGES],
                model_ref=str(snapshot.get("model_ref") or ""),
                credential_id=str(snapshot.get("credential_id") or ""), run_id=run_id)
            if vision_result.get("issue"):
                issues.append({"source_id": tagged_source_id, "reason": vision_result["issue"]})
            if vision_result.get("observations"):
                brief["image_observations"] = {
                    "source_id": tagged_source_id,
                    "note": "视觉模型仅描述实际可见形态；需人工核对，不作为性能或市场规模证据。",
                    "images": vision_result["observations"]}
        brief, shortened, brief_chars = o._fit_prompt_to_char_budget(brief, o.MAX_ANALYSIS_TEXT_CHARS)
        model_ref = str(snapshot.get("reasoning_model_ref") or "")
        provider = str(snapshot.get("reasoning_provider") or "")
        credential_id = str(snapshot.get("reasoning_credential_id") or "")
        row.update(source_manifest=manifest, source_read_issues=issues,
                   input_routing={"mode": "reasonix_skill_holistic_analysis", "source_count": len(materials),
                                  "material_truncated": bool(shortened or remaining <= 0 or
                                                             any(item.get("truncated") for item in compacted)),
                                  "text_chars": brief_chars, "text_chars_max": o.MAX_ANALYSIS_TEXT_CHARS},
                   model_attempted=True, phase="reasonix_research",
                   visual_input_status="analyzed" if vision_result.get("observations") else "not_analyzed",
                   model_call_ledger=([{"stage": "image_observation",
                                        "status": "completed" if vision_result.get("observations") else "failed",
                                        "model_ref": snapshot.get("model_ref"),
                                        "image_count": vision_result.get("image_count", 0)}]
                                      if image_paths and use_images else []) +
                                     [{"stage": "reasonix_research", "status": "running",
                                       "model_ref": model_ref, "request_count": None,
                                       "agent_steps_max": reasonix_research.MAX_STEPS}],
                   usage_estimate={"agent_steps_max": reasonix_research.MAX_STEPS,
                                   "search_requests_max": 6, "web_pages_max": 8,
                                   "input_tokens": estimate_text_tokens(json.dumps(brief, ensure_ascii=False)),
                                   "image_count": vision_result.get("image_count", 0),
                                   "cost_status": "unknown"})
        o._save(archive_id, run_id, row)
        event_id = start_usage_event(kind="agent_turn", module="product_archive", task_id=run_id,
                                     task_title="选品档案 Reasonix 综合市场研究", model=model_ref,
                                     provider=provider, attempt=1,
                                     metadata={"stage": "reasonix_research",
                                               "agent_steps_max": reasonix_research.MAX_STEPS})
        started = time.perf_counter()
        result = reasonix_research.run_research(
            archive_id, run_id, model_ref=model_ref, provider=provider,
            credential_id=credential_id, brief=brief,
            image_paths=[])
        usage = _record_cli_usage(row, result, image_count=vision_result.get("image_count", 0))
        finish_usage_event(event_id, status="completed",
                           duration_ms=round((time.perf_counter() - started) * 1000), usage=usage)
        event_id = None
        row["model_call_ledger"][-1].update(status="completed", num_turns=result.get("num_turns"),
                                              input_tokens=row["usage"]["input_tokens"],
                                              output_tokens=row["usage"]["output_tokens"])
        row["parse_repair"] = result.get("parse_repair")
        issues.extend({"source_id": tagged_source_id, "reason": issue}
                      for issue in result.get("image_issues") or [])
        citations = result["citations"]
        for citation in citations:
            evidence[citation["evidence_id"]] = {"source_type": "web"}
            manifest.append({"id": citation["evidence_id"], "type": "web", "name": citation.get("title"),
                             "status": "completed", "selected": True, "date": citation.get("retrieved_at"),
                             "coverage": citation.get("url")})
        apply_research_coverage(row, result)
        analysis = result["analysis"]
        row.update(model_response_status="parsed", model_returned=o._plain_object(analysis),
                   model_returned_fields=sorted(str(key)[:120] for key in analysis))
        o._save(archive_id, run_id, row)
        o._set_phase(archive_id, run_id, "validating")
        suggestions, diagnostics = o._build_analysis(run_id, analysis, evidence, citations,
                                                     row["category_context"], row["data_month_context"], months)
        row["diagnostics"] = diagnostics
        row["filtered_reasons"] = diagnostics.get("filtered", [])
        if diagnostics.get("completion_blockers"):
            raise o.ModelOutputError("Reasonix 返回的分析结构不完整")
        market_analysis = diagnostics.get("market_analysis")
        if not suggestions and not market_analysis:
            raise o.ModelOutputError("Reasonix 没有返回可用分析内容")
        needs_validation = {}
        seasonal = o._seasonality_validation(months)
        if seasonal:
            needs_validation["seasonality"] = {**seasonal,
                                                "usage_period_allowed": True, "sales_peak_allowed": False}
        row.update(status="ready", phase="ready", suggestions=suggestions, market_analysis=market_analysis,
                   error=None, citations=citations, needs_validation=needs_validation)
    except Exception as exc:
        recorded = reasonix_research.read_recorded_result(archive_id, run_id)
        envelope = recorded["envelope"] if recorded else None
        recorded_usage = (_record_cli_usage(row, envelope,
                          image_count=(row.get("usage") or {}).get("image_count", 0))
                          if envelope else None)
        if event_id is not None:
            finish_usage_event(event_id, status="failed",
                               duration_ms=round((time.perf_counter() - started) * 1000)
                                if started is not None else None, usage=recorded_usage)
        attempted = bool(row.get("model_attempted"))
        diagnostics = row.get("diagnostics") if isinstance(row.get("diagnostics"), dict) else {}
        cli_success = bool(envelope and envelope.get("subtype") == "success"
                           and not envelope.get("is_error"))
        stage = "validating" if cli_success or row.get("model_response_status") == "parsed" else (
            "reasonix_research" if attempted else "source_preparation")
        explanation = ("Reasonix 已返回结果，但分析结构未通过解析或校验；可检查离线恢复。"
                       if cli_success else "Reasonix 研究未完成；请核对模型、检索工具及已知用量。")
        row.update(status="failed", phase=stage,
                   error=o._safe_error(exc) if attempted else "资料准备失败，尚未启动 Reasonix",
                   suggestions={}, market_analysis=None, diagnostics={**diagnostics, "stage": stage,
                                                 "parse_or_analysis_error": type(exc).__name__,
                                                 "message": explanation})
        if row.get("model_call_ledger"):
            if row["model_call_ledger"][-1].get("status") == "running":
                row["model_call_ledger"][-1].update(
                    status="output_invalid" if cli_success else "failed",
                    num_turns=envelope.get("num_turns") if envelope else None,
                    input_tokens=(row.get("usage") or {}).get("input_tokens"),
                    output_tokens=(row.get("usage") or {}).get("output_tokens"),
                    error_type=type(exc).__name__)
    o._finish(archive_id, run_id, row)
