"""Persistent, idempotent overview suggestion jobs."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import re
import math
import unicodedata
from urllib.parse import urlparse
import uuid
from datetime import datetime, timezone

from . import service

_LOCK = threading.RLock()
_ACTIVE: dict[str, threading.Thread] = {}


def _root():
    return service.ARCHIVES.parent / "overview-drafts"


def _path(archive_id: str, run_id: str):
    return _root() / archive_id / f"{run_id}.json"


def _read(archive_id: str, run_id: str):
    path = _path(archive_id, run_id)
    if not path.is_file():
        raise ValueError("选品档案草稿不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(archive_id: str, run_id: str, row: dict):
    path = _path(archive_id, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _evidence_fingerprint(archive: dict):
    source = {"dataset_id": archive.get("dataset_id"), "snapshot": archive.get("source_snapshot"),
              "integrations": archive.get("integrations", {})}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def create(archive_id: str, body: dict):
    archive = service._read(archive_id)
    category = str(body.get("category_context") or "").strip()[:300]
    month = str(body.get("data_month_context") or "").strip()[:20]
    if not category:
        raise ValueError("请填写用于建议生成的品类")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("数据月份格式应为 YYYY-MM")
    local_only = body.get("local_only", False)
    if not isinstance(local_only, bool):
        raise ValueError("local_only 必须是布尔值")
    source_urls = body.get("source_urls") or []
    if not isinstance(source_urls, list) or len(source_urls) > 3 or any(not isinstance(url, str) for url in source_urls):
        raise ValueError("source_urls 必须是最多 3 个 HTTPS 链接")
    source_urls = list(dict.fromkeys(url.strip() for url in source_urls if url.strip()))
    for url in source_urls:
        parsed = urlparse(url)
        try:
            valid_port = parsed.port in (None, 443)
        except ValueError:
            valid_port = False
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not valid_port):
            raise ValueError("资料链接需为公开 HTTPS 网页")
    if local_only and source_urls:
        raise ValueError("local_only 模式不能包含网页资料链接")
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force 必须是布尔值")
    fingerprint = _evidence_fingerprint(archive)
    key = hashlib.sha256(json.dumps([archive_id, category, month, local_only, source_urls, fingerprint],
                                   ensure_ascii=False).encode()).hexdigest()
    root = _root() / archive_id
    root.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if not force:
            for path in root.glob("*.json"):
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if saved.get("request_key") == key and saved.get("status") in {"running", "ready", "web_unavailable"}:
                    if saved.get("status") == "running" and saved["id"] not in _ACTIVE:
                        if saved.get("model_attempted"):
                            saved.update(status="failed", error="请求中断，费用状态待核对；为避免重复计费已停止自动重试")
                            _save(archive_id, saved["id"], saved)
                        else:
                            _launch(archive_id, saved["id"])
                    return {"id": saved["id"], "status": saved["status"]}
        run_id = uuid.uuid4().hex
        record = {"id": run_id, "archive_id": archive_id, "status": "running", "suggestions": {},
                  "error": None, "usage": None, "request_key": key, "category_context": category,
                  "data_month_context": month, "local_only": local_only,
                  "source_urls": source_urls,
                  "evidence_fingerprint": fingerprint,
                  "created_at": datetime.now(timezone.utc).isoformat(), "updated_at": None}
        _save(archive_id, run_id, record)
        _launch(archive_id, run_id)
        return {"id": run_id, "status": "running"}


def _launch(archive_id: str, run_id: str):
    thread = threading.Thread(target=_worker, args=(archive_id, run_id), daemon=True)
    _ACTIVE[run_id] = thread
    thread.start()


def _safe_error(exc: Exception):
    # Provider exceptions can contain request URLs or snippets; expose only the class.
    return f"任务失败（{type(exc).__name__}）"


def _parse_response_content(content):
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    if not isinstance(content, str):
        raise ValueError("模型没有返回文本 JSON")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("模型 JSON 顶层必须是对象")
    return parsed


def _build_suggestions(run_id: str, generated: dict, evidence: dict, sources: list[dict],
                       category: str, month: str, comparable_months: list[str] | None = None):
    allowed_fields = {"product_name", "product_function", "target_audience", "cultural_context",
                      "seasonality", "representative_keywords"}
    links_by_id = {item.get("evidence_id"): item for item in sources if item.get("evidence_id")}
    suggestions = {}
    for field, value in generated.items():
        if field not in allowed_fields or not isinstance(value, dict):
            continue
        if field == "seasonality" and len(set(comparable_months or [])) < 3:
            continue
        text = str(value.get("text") or "").strip()[:4000]
        refs = value.get("evidence_refs")
        if not text or not isinstance(refs, list):
            continue
        valid_refs = list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref in evidence))
        if not valid_refs:
            continue
        source_types = {evidence[ref].get("source_type") for ref in valid_refs}
        source_type = next(iter(source_types)) if len(source_types) == 1 else "overview_draft"
        citations = [links_by_id[ref] for ref in valid_refs if ref in links_by_id]
        ident = hashlib.sha256((run_id + field + text).encode()).hexdigest()[:24]
        suggestions[field] = [{"id": ident, "text": text, "source_type": source_type,
            "source_ids": valid_refs, "scope": {"category_context": category, "data_month_context": month},
            "links": citations, "updated_at": datetime.now(timezone.utc).isoformat()}]
    return suggestions


def _comparable_demand_months(archive: dict) -> list[str]:
    """Months with archived keyword demand measurements; a single sample month is not seasonal evidence."""
    integration = ((archive.get("integrations") or {}).get("keyword_research") or {})
    months = set()
    for source in integration.get("sources") or []:
        if not isinstance(source, dict) or not source.get("keyword_count"):
            continue
        month = str(source.get("data_month") or "")
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            months.add(month)
    for term in integration.get("representative_terms") or []:
        if not isinstance(term, dict) or term.get("searches") is None:
            continue
        month = str(term.get("data_month") or "")
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            months.add(month)
    return sorted(months)


def _seasonality_validation(comparable_months: list[str]):
    months = sorted(set(comparable_months))
    if len(months) >= 3:
        return None
    return {"status": "pending_validation",
            "reason": "至少需要3个不同月份的可比销量或搜索需求证据",
            "observed_months": months}


def _research_sources(category: str, marketplace: str, keyword_terms: list[str] | None = None,
                      source_urls: list[str] | None = None):
    """Search one category query and, if needed, one archive-keyword fallback."""
    from .. import knowledge
    from ..external_effects import assert_allowed
    assert_allowed("external_research")
    query = " ".join(part for part in (category, marketplace) if part).strip()
    if not query:
        return []
    sources = []
    english_terms = []
    for term in keyword_terms or []:
        term = str(term or "").strip()
        if re.search(r"[A-Za-z]{3,}", term) and term.casefold() not in {item.casefold() for item in english_terms}:
            english_terms.append(term)
    queries = [(query, category)]
    fallback = " ".join([*english_terms[:3], marketplace, "site:gov OR site:edu"]).strip()
    if fallback and fallback.casefold() != query.casefold():
        queries.append((fallback, " ".join(english_terms[:3])))

    for search_query, relevance_text in queries[:2]:
        try:
            search_html = knowledge.fetch("https://html.duckduckgo.com/html/", {"q": search_query})
            candidates = knowledge.search_results(search_html, search_query)
        except Exception:
            continue
        tokens = set(re.findall(r"[a-z0-9]{3,}|[\u3400-\u9fff]{2,}",
                                unicodedata.normalize("NFKC", relevance_text).casefold()))
        for word in list(tokens):
            if len(word) > 2 and re.search(r"[\u3400-\u9fff]", word):
                tokens.update(word[index:index + 2] for index in range(len(word) - 1))
        for candidate in candidates[:3]:
            try:
                url = knowledge.check_url(candidate["url"])
                page_text = knowledge.plain(knowledge.fetch(url))
            except Exception:
                continue
            if not page_text:
                continue
            normalized = unicodedata.normalize("NFKC", page_text).casefold()
            if tokens:
                matched_tokens = [token for token in tokens if token in normalized]
                if len(matched_tokens) < max(1, math.ceil(len(tokens) * 0.25)):
                    continue
                positions = [normalized.find(token) for token in matched_tokens]
                excerpt = page_text[max(0, min(positions) - 350):][:1500]
            else:
                continue
            evidence_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:16]
            sources.append({"evidence_id": evidence_id, "title": candidate.get("title", ""), "url": url,
                            "excerpt": excerpt, "publisher": candidate.get("publisher"),
                            "retrieved_at": datetime.now(timezone.utc).isoformat()})
        if sources:
            break
    for requested_url in (source_urls or [])[:3]:
        try:
            url = knowledge.check_url(requested_url)
            page_text = knowledge.plain(knowledge.fetch(url))
        except Exception:
            continue
        if not page_text:
            continue
        parsed = urlparse(url)
        path_words = re.findall(r"[a-z0-9]{3,}",
                                unicodedata.normalize("NFKC", parsed.path).casefold())
        path_words = [word for word in path_words if word not in {"index", "html", "htm", "amp"}]
        if not path_words:
            continue
        normalized = unicodedata.normalize("NFKC", page_text).casefold()
        matched = [word for word in dict.fromkeys(path_words) if word in normalized]
        if path_words and len(matched) < max(1, math.ceil(len(set(path_words)) * 0.5)):
            continue
        excerpt = page_text[:1500]
        if matched:
            start = max(0, min(normalized.find(word) for word in matched) - 350)
            excerpt = page_text[start:start + 1500]
        evidence_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:16]
        if any(item.get("url") == url for item in sources):
            continue
        sources.append({"evidence_id": evidence_id, "title": parsed.hostname or "",
                        "url": url, "excerpt": excerpt, "publisher": parsed.hostname,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "explicit_reference": True})
    return sources


def _worker(archive_id: str, run_id: str):
    row = _read(archive_id, run_id)
    try:
        archive = service._read(archive_id)
        citations = []
        if not row["local_only"]:
            try:
                keyword_terms = [item.get("keyword", "") for item in
                    (((archive.get("integrations") or {}).get("keyword_research") or {}).get("representative_terms") or [])
                    if isinstance(item, dict)]
                citations = _research_sources(row["category_context"], archive.get("marketplace", ""),
                                              keyword_terms, row.get("source_urls"))
            except Exception:
                citations = []
            if not citations:
                row.update(status="web_unavailable", suggestions={}, error="公开网页资料检索不可用；可选择仅用本地样本再次生成")
                _finish(archive_id, run_id, row)
                return
        from .. import model_settings
        from ..external_effects import assert_allowed
        from ..runtime_isolation import credential_selection
        from ..batch import provider_config
        import httpx
        model_ref, _ = model_settings.defaults()
        provider, credential_id = credential_selection(model_ref)
        endpoint, key, model = provider_config(model_ref, credential_id)
        assert_allowed("model_api")
        sample = _compact_sample(archive)
        comparable_months = _comparable_demand_months(archive)
        prompt = {"category_context": row["category_context"], "marketplace": archive.get("marketplace"),
                  "data_month_context": row["data_month_context"], "sample_summary": sample,
                  "sources": citations,
                  "evidence_refs": [{"id": "local_sample:aggregate", "type": "local_sample"},
                    *sample.get("keyword_evidence", []),
                    *[{"id": item["evidence_id"], "type": "public_web",
                       "url": item["url"], "excerpt": item["excerpt"]} for item in citations]],
                  "comparable_demand_months": comparable_months,
                  "instruction": "只输出 JSON 对象。每个字段值必须是 {text: 字符串, evidence_refs: [上方证据ID]}。字段为 product_name, product_function, target_audience, cultural_context, seasonality, representative_keywords。每个非空建议必须引用至少一个实际支持该字段的证据ID；不得复用不相关证据。无充分证据时 text 为空且 evidence_refs 为空。季节性只有在 comparable_demand_months 至少有3个不同月份时才能提出；样本单月或一般性网页资料不能证明销量旺季。"}
        from ..usage import start_usage_event, finish_usage_event, provider_name
        event_id = start_usage_event(kind="model", module="product_archive", task_id=run_id,
                                     task_title="选品档案概览建议", model=model_ref,
                                     provider=provider_name(model_ref, endpoint), attempt=1, request_text=prompt)
        row["model_attempted"] = True
        _save(archive_id, run_id, row)
        try:
            response = httpx.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                      "temperature": 0.2, "max_tokens": 4000,
                      "response_format": {"type": "json_object"}}, timeout=90)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            generated = _parse_response_content(content)
            finish_usage_event(event_id, status="completed", usage=data.get("usage"))
            row["usage"] = data.get("usage")
        except Exception:
            finish_usage_event(event_id, status="failed")
            raise
        evidence = {"local_sample:aggregate": {"source_type": "local_sample"}}
        for item in sample.get("keyword_evidence", []):
            evidence[item["id"]] = {"source_type": "keyword_research"}
        for item in citations:
            evidence[item["evidence_id"]] = {"source_type": "web"}
        suggestions = _build_suggestions(run_id, generated, evidence, citations,
                                         row["category_context"], row["data_month_context"], comparable_months)
        needs_validation = {}
        seasonality_validation = _seasonality_validation(comparable_months)
        if seasonality_validation:
            needs_validation["seasonality"] = seasonality_validation
        row.update(status="ready", suggestions=suggestions, error=None, citations=citations,
                   needs_validation=needs_validation)
    except Exception as exc:
        row.update(status="failed", error=_safe_error(exc), suggestions={})
    _finish(archive_id, run_id, row)


def _compact_sample(archive: dict):
    dataset, rows = service._source(archive["dataset_id"])
    mapping = archive.get("mapping") or {}
    from collections import Counter
    values = {}
    for role, header in mapping.items():
        if not header or role in {"asin", "url", "brand", "product_name"}:
            continue
        observed = [str((row.get("raw") or {}).get(header)).strip() for row in rows
                    if (row.get("raw") or {}).get(header) not in (None, "")]
        if observed:
            counts = Counter(observed)
            values[role] = {"distinct_values": len(counts), "top_values": [
                {"value": value[:100], "rows": count} for value, count in counts.most_common(8)]}
    keywords = (((archive.get("integrations") or {}).get("keyword_research") or {})
                .get("representative_terms") or [])
    keyword_evidence = [{"id": "keyword:" + str(item.get("source_run_id") or "unknown") + ":" +
                         hashlib.sha256(str(item.get("keyword") or "").encode()).hexdigest()[:10],
                         "keyword": str(item.get("keyword") or "")[:100]}
                        for item in keywords[:10] if isinstance(item, dict) and item.get("keyword")]
    return {"sample_rows": len(rows), "marketplace": archive.get("marketplace"),
            "category_context": archive.get("category"), "data_month": archive.get("data_month"),
            "field_coverage": {role: sum(1 for row in rows if (row.get("raw") or {}).get(header) not in (None, ""))
                               for role, header in mapping.items() if header},
            "category_level_distributions": values,
            "keyword_research_terms": [item["keyword"] for item in keyword_evidence],
            "keyword_evidence": keyword_evidence,
            "keyword_research_note": "关键词候选可能包含跨品类噪声，不得据此推断样本品类。",
            "note": "只发送汇总分布，不发送原始商品行、ASIN、商品名、品牌名或链接。"}


def _finish(archive_id: str, run_id: str, row: dict):
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    if row.get("status") == "ready" and row.get("suggestions"):
        archive = service._read(archive_id)
        fields = archive.setdefault("overview_suggestions", {})
        for field, items in row["suggestions"].items():
            target = fields.setdefault(field, [])
            known = {item.get("id") for item in target if isinstance(item, dict)}
            target.extend(item for item in items if item.get("id") not in known)
        service._save(archive)
    _save(archive_id, run_id, row)
    with _LOCK:
        _ACTIVE.pop(run_id, None)


def get(archive_id: str, run_id: str):
    row = _read(archive_id, run_id)
    if row.get("archive_id") != archive_id:
        raise ValueError("选品档案草稿不存在")
    if row.get("status") == "running" and run_id not in _ACTIVE:
        if row.get("model_attempted"):
            row.update(status="failed", error="请求中断，费用状态待核对；为避免重复计费已停止自动重试")
            _finish(archive_id, run_id, row)
        else:
            with _LOCK:
                if run_id not in _ACTIVE:
                    _launch(archive_id, run_id)
    return {key: value for key, value in row.items() if key != "request_key"}
