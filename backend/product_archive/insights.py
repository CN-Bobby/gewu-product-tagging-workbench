"""Pure merge helpers for generated Product Archive insights.

These functions deliberately consume already-scoped run data. They do not read
files, call models, or make network requests, and they never copy review text or
ASINs into an archive.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from statistics import median
from typing import Any


_KEYWORD_METRICS = ("searches", "bid", "top3ClickingRate", "top3ConversionRate", "purchaseRate")
_OVERVIEW_LIMIT = 4000
_SUGGESTION_LIMIT = 900


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    return " ".join(text.split()).casefold()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = _text(value).replace(",", "")
        if not text:
            return None
        match = re.fullmatch(r"\+?(?:\d+(?:\.\d*)?|\.\d+)", text)
        if not match:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _timestamp(*sources: dict) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("updated_at", "completed_at", "created_at"):
            value = _text(source.get(key))
            if value:
                return value
    return datetime.now(timezone.utc).isoformat()


def _ensure_archive(archive: dict) -> dict:
    result = copy.deepcopy(archive) if isinstance(archive, dict) else {}
    result.setdefault("overview", {})
    result.setdefault("overview_sources", {})
    result.setdefault("overview_suggestions", {})
    result.setdefault("integrations", {})
    return result


def _may_write_overview(archive: dict, field: str, source_type: str) -> bool:
    value = (archive.get("overview") or {}).get(field)
    if not _text(value):
        return True
    provenance = (archive.get("overview_sources") or {}).get(field) or {}
    return provenance.get("source_type") == source_type


def _write_overview(archive: dict, field: str, value: str, source_type: str,
                    source_ids: list[str], updated_at: str) -> None:
    if not value or not _may_write_overview(archive, field, source_type):
        return
    if len(value) > _OVERVIEW_LIMIT:
        value = value[:_OVERVIEW_LIMIT].rstrip()
    archive["overview"].setdefault(field, "")
    archive["overview"][field] = value
    archive["overview_sources"][field] = {
        "source_type": source_type,
        "source_ids": list(dict.fromkeys(item for item in source_ids if item)),
        "updated_at": updated_at,
    }


def _suggest_overview(archive: dict, field: str, value: str, source_type: str,
                      source_ids: list[str], updated_at: str, scope: dict | None = None,
                      links: list[dict] | None = None) -> None:
    if not value:
        return
    payload = {"field": field, "text": value[:_OVERVIEW_LIMIT], "source_type": source_type,
               "source_ids": list(dict.fromkeys(item for item in source_ids if item)),
               "scope": scope or {}, "links": links or [], "updated_at": updated_at}
    ident = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    item = {"id": ident, **payload}
    items = archive["overview_suggestions"].setdefault(field, [])
    if not any(existing.get("id") == ident for existing in items if isinstance(existing, dict)):
        items.append(item)
    return item


def _median(rows: list[dict], field: str) -> float | None:
    values = [row[field] for row in rows if row.get(field) is not None]
    return round(float(median(values)), 6) if values else None


def merge_keyword_insights(archive: dict, runs: list[dict]) -> dict:
    """Merge representative keyword signals from caller-scoped completed runs.

    Search values are kept per selected term and summarized by median. They are
    never added together, because overlapping terms and runs are not additive
    measures of market demand.
    """
    result = _ensure_archive(archive)
    candidates: list[dict] = []
    sources: list[dict] = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        run_id = _text(run.get("run_id"))
        data_month = _text(run.get("data_month")) or None
        run_count = 0
        for raw in run.get("keywords") or []:
            if not isinstance(raw, dict):
                continue
            relevance = _text(raw.get("relevance")).upper()
            if relevance not in {"A", "B"} or raw.get("is_noise") is not False:
                continue
            if _text(raw.get("traffic_type")).casefold() == "wrong_category":
                continue
            keyword = _text(raw.get("keyword"))
            normalized = _normalized(keyword)
            if not normalized:
                continue
            parsed = {field: _number(raw.get(field)) for field in _KEYWORD_METRICS}
            # A usable representative needs a parseable demand signal; malformed
            # optional CPC/concentration fields stay null rather than being guessed.
            if parsed["searches"] is None:
                continue
            row = {
                "keyword": keyword,
                **parsed,
                "relevance": relevance,
                "traffic_type": _text(raw.get("traffic_type")) or None,
                "data_month": data_month,
                "source_run_id": run_id,
                "_normalized": normalized,
            }
            candidates.append(row)
            run_count += 1
        if run_id:
            sources.append({"run_id": run_id, "data_month": data_month, "keyword_count": run_count})

    candidates.sort(key=lambda row: (
        0 if row["relevance"] == "A" else 1,
        -(row["searches"] if row["searches"] is not None else -1),
        -(row["purchaseRate"] if row["purchaseRate"] is not None else -1),
        row["_normalized"],
        row["source_run_id"],
    ))
    selected: list[dict] = []
    seen: set[str] = set()
    for row in candidates:
        if row["_normalized"] in seen:
            continue
        seen.add(row["_normalized"])
        selected.append({key: value for key, value in row.items() if not key.startswith("_")})
        if len(selected) == 10:
            break

    updated_at = _timestamp(*(runs or []), archive if isinstance(archive, dict) else {})
    result["integrations"]["keyword_research"] = {
        "updated_at": updated_at,
        "sources": sources,
        "representative_terms": selected,
        "summary": {
            "representative_term_count": len(selected),
            "searches_median": _median(selected, "searches"),
            "bid_median": _median(selected, "bid"),
            "top3_click_median": _median(selected, "top3ClickingRate"),
            "top3_conversion_median": _median(selected, "top3ConversionRate"),
            "non_additive": True,
        },
    }
    source_ids = [row["source_run_id"] for row in selected if row.get("source_run_id")]
    if selected:
        _suggest_overview(result, "representative_keywords",
                          "\n".join(row["keyword"] for row in selected),
                          "keyword_research", source_ids, updated_at,
                          {"data_months": sorted({row["data_month"] for row in selected if row.get("data_month")})})
    return result


def _bounded_unique_text(values: list[Any], limit: int = 3) -> str:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(_text(value).split())
        key = _normalized(text)
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(text)
        if len(selected) >= limit:
            break
    return "；".join(selected)[:_SUGGESTION_LIMIT].rstrip("； ")


def _count(value: Any) -> int:
    parsed = _number(value)
    return int(parsed) if parsed is not None else 0


def _optional_count(value: Any) -> int | None:
    return None if value is None else _count(value)


def merge_voc_insights(archive: dict, run: dict, analysis: dict,
                       source_context: dict) -> dict:
    """Merge compact Amazon VOC summaries without retaining review evidence."""
    result = _ensure_archive(archive)
    run = run if isinstance(run, dict) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    source_context = source_context if isinstance(source_context, dict) else {}
    segments = [item for item in analysis.get("segments", [])
                if isinstance(item, dict) and _text(item.get("status")).casefold() == "formal"]

    def review_count(segment: dict) -> int:
        metrics = segment.get("metrics") if isinstance(segment.get("metrics"), dict) else {}
        return _count(metrics.get("review_count", segment.get("review_count")))

    segments.sort(key=lambda item: (-review_count(item), _text(item.get("segment_id"))))
    top_segments = segments[:3]
    product_suggestion = _bounded_unique_text([item.get("jtbd_statement") for item in top_segments])
    audience_suggestion = _bounded_unique_text([item.get("persona_summary") for item in top_segments])
    validation = analysis.get("validation") if isinstance(analysis.get("validation"), dict) else {}
    updated_at = _timestamp(run, analysis, archive if isinstance(archive, dict) else {})
    run_id = _text(run.get("run_id")) or _text(run.get("id")) or None
    scope = {
        key: source_context.get(key)
        for key in ("analysis_scope", "source_job_status", "completed_asin_count",
                    "empty_comment_asin_count", "source_linkage")
        if source_context.get(key) is not None
    }
    integration = {
        "updated_at": updated_at,
        "run_id": run_id,
        "revision": _count(run.get("revision", run.get("current_revision"))),
        "reviews_sha256": _text(run.get("reviews_sha256")) or None,
        "review_count": _optional_count(source_context.get("review_count")),
        "requested_asin_count": _optional_count(source_context.get("requested_asin_count")),
        "comment_asin_count": _optional_count(source_context.get("comment_asin_count")),
        "failed_asin_count": _optional_count(source_context.get("failed_asin_count")),
        "quality_all_green": validation.get("all_green") is True,
        "segments_used": [
            {"segment_id": _text(item.get("segment_id")) or None,
             "review_count": review_count(item)}
            for item in top_segments
        ],
        "product_function_suggestion": product_suggestion,
        "target_audience_suggestion": audience_suggestion,
        "analysis_scope": scope,
    }
    result["integrations"]["amazon_voc"] = integration
    if product_suggestion:
        _suggest_overview(result, "product_function", product_suggestion,
                        "amazon_voc", [run_id] if run_id else [], updated_at)
    if audience_suggestion:
        _suggest_overview(result, "target_audience", audience_suggestion,
                        "amazon_voc", [run_id] if run_id else [], updated_at)
    return result
