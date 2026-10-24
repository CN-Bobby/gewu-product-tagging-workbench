"""Concise, evidence-aware segment cards for Product Archive analysis v3."""

from __future__ import annotations

import json
import re


FIELDS = ("product", "audience", "scenarios", "culture", "seasonality")
FIELD_LABELS = dict(zip(FIELDS, ("产品是什么", "谁使用", "使用情境", "文化背景", "季节性")))
MAX_SEGMENTS = 30
MAX_ITEMS = 6
MAX_OVERVIEW_POINTS = 4


def _claim(raw, evidence: dict, where: str, filtered: list[dict], months: set[str]):
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
        filtered.append({"field": where, "reason": "缺少有效结论文字"})
        return None
    text = raw["text"].strip()
    if len(text) > 3000:
        filtered.append({"field": where, "reason": "结论超过单条长度上限，保留原文供离线核对"})
        return None
    refs = raw.get("evidence_refs", [])
    if not isinstance(refs, list):
        refs = []
    kept = list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref in evidence))
    if len(kept) != len(refs):
        filtered.append({"field": where, "reason": "忽略无效资料编号"})
    basis = raw.get("basis")
    note = str(raw.get("basis_note") or "").strip()
    if basis not in {"observation", "synthesis", "inference"} or (not kept and (basis != "inference" or not note)):
        filtered.append({"field": where, "reason": "依据无效或无来源推断缺少说明"})
        return None
    kind = str(raw.get("kind") or "").strip()
    measured = list(dict.fromkeys(m for m in raw.get("measured_months", []) if isinstance(m, str))) if isinstance(raw.get("measured_months", []), list) else []
    if where.endswith("seasonality") or ".seasonality[" in where:
        if kind not in {"possible_use_period", "search_demand_peak", "sales_peak"}:
            filtered.append({"field": where, "reason": "季节性未区分可能使用时段、搜索需求高点与实测销量旺季"})
            return None
        if kind in {"search_demand_peak", "sales_peak"}:
            allowed = ({"keywords", "keyword_research"} if kind == "search_demand_peak"
                       else {"sales_month_series"})
            cited = set()
            for ref in kept:
                source = evidence[ref]
                if not isinstance(source, dict) or source.get("source_type") not in allowed:
                    continue
                if kind == "search_demand_peak" and source.get("metric") not in {None, "monthly_searches"}:
                    continue
                values = source.get("data_months")
                if isinstance(values, list):
                    cited.update(value for value in values if isinstance(value, str))
                if isinstance(source.get("data_month"), str):
                    cited.add(source["data_month"])
            if len(months) < 3 or len(set(measured)) < 3 or not set(measured).issubset(months) or not set(measured).issubset(cited):
                label = "搜索需求高点" if kind == "search_demand_peak" else "实测销量旺季"
                filtered.append({"field": where, "reason": f"{label}须有至少三个可比月份及对应同口径来源"})
                return None
            sales_peak_claim = any(
                re.search(r"销售旺季|销量峰值|销售高峰", clause)
                and not re.search(r"不等于|不代表|并非|不是|不能证明|无法证明|未证明|不能视为|不构成", clause)
                for clause in re.split(r"[，。；;]", text)
            )
            if kind == "search_demand_peak" and sales_peak_claim:
                filtered.append({"field": where, "reason": "搜索量不能写成商品销量旺季"})
                return None
        elif re.search(r"(?<!无法确认)(销售旺季在|销量峰值在|销售高峰在)", text):
            filtered.append({"field": where, "reason": "可能使用时段不得断言实际销售旺季"})
            return None
    classification = raw.get("classification") if raw.get("classification") in {"supported", "hypothesis"} else "hypothesis"
    if not kept:
        classification = "hypothesis"
    return {"text": text, "basis": basis, "basis_note": note, "evidence_refs": kept,
            "limits": str(raw.get("limits") or "").strip(), "classification": classification,
            **({"kind": kind, "measured_months": measured} if "seasonality" in where else {})}


def _needs(raw):
    if not isinstance(raw, list):
        return []
    return [{"id": str(item.get("id") or f"V{i}")[:40], "text": item["text"].strip()[:1000]}
            for i, item in enumerate(raw[:50], 1)
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()]


def normalize(generated: dict, evidence: dict, comparable_months: list[str] | None = None):
    """Keep valid claims and explain every missing field without rewriting model text."""
    filtered, gaps = [], []
    months = set(comparable_months or [])
    overview = _claim(generated.get("overview"), evidence, "overview", filtered, months)
    if overview:
        raw_points = generated["overview"].get("points", [])
        if not isinstance(raw_points, list):
            filtered.append({"field": "overview.points", "reason": "综述条目不是列表，已保留原始回答供核对"})
            raw_points = []
        if len(raw_points) > MAX_OVERVIEW_POINTS:
            filtered.append({"field": "overview.points", "reason": "综述条目超过四条，额外条目保留于原始回答"})
        points = []
        for i, raw in enumerate(raw_points[:MAX_OVERVIEW_POINTS], 1):
            label = str(raw.get("label") or "").strip() if isinstance(raw, dict) else ""
            if not label or len(label) > 16:
                filtered.append({"field": f"overview.points[{i}]", "reason": "综述条目标题缺失或过长"})
                continue
            point = _claim(raw, evidence, f"overview.points[{i}]", filtered, months)
            if point:
                points.append({"label": label, **point})
        if points:
            overview["points"] = points
    segments, seen = [], set()
    raw_segments = generated.get("segments") if isinstance(generated.get("segments"), list) else []
    if len(raw_segments) > MAX_SEGMENTS:
        filtered.append({"field": "segments", "reason": "超出细分数量上限的内容未纳入；原始回答已保存"})
    for i, raw in enumerate(raw_segments[:MAX_SEGMENTS], 1):
        if not isinstance(raw, dict):
            continue
        sid, name = str(raw.get("id") or f"S{i}").strip()[:40], str(raw.get("name") or "").strip()[:160]
        if not sid or sid in seen or not name:
            filtered.append({"field": f"segments[{i}]", "reason": "细分编号重复或名称缺失"})
            continue
        seen.add(sid)
        raw_fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
        fields = {}
        for key in FIELDS:
            group = raw_fields.get(key) if isinstance(raw_fields.get(key), dict) else {}
            raw_items = group.get("items") if isinstance(group.get("items"), list) else []
            if len(raw_items) > MAX_ITEMS:
                filtered.append({"field": f"segments[{sid}].{key}", "reason": "超出单字段条目上限的内容未纳入；原始回答已保存"})
            items = [result for j, item in enumerate(raw_items[:MAX_ITEMS], 1)
                     if (result := _claim(item, evidence, f"segments[{sid}].{key}[{j}]", filtered, months))]
            missing = str(group.get("missing_reason") or "").strip()[:500]
            if not items:
                missing = missing or "现有资料与研究未形成可靠结论。"
                gaps.append({"field": f"segments[{sid}].{key}", "reason": missing})
            fields[key] = {"items": items, "missing_reason": missing}
        segments.append({"id": sid, "name": name, "fields": fields,
                         "needs_validation": _needs(raw.get("needs_validation"))})
    blockers = []
    if not overview:
        blockers.append("缺少有依据的当前样本综述。")
    if not segments:
        blockers.append("缺少有效细分市场。")
    elif not any(segment["fields"]["product"]["items"] and
                 (segment["fields"]["audience"]["items"] or segment["fields"]["scenarios"]["items"])
                 for segment in segments):
        blockers.append("细分缺少有效的产品描述和使用对象或情境。")
    analysis = {"schema_version": 3, "overview": overview, "segments": segments,
                "needs_validation": _needs(generated.get("needs_validation"))}
    return {}, {"completion_blockers": blockers, "filtered": filtered,
                "market_analysis": analysis, "missing_fields": gaps, "module_gaps": gaps,
                "quality_warnings": []}


def validate(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise ValueError("market_analysis 必须使用 schema_version 3")
    if len(json.dumps(value, ensure_ascii=False)) > 1_000_000:
        raise ValueError("market_analysis 超过内容限制")
    overview, segments, needs = value.get("overview"), value.get("segments"), value.get("needs_validation")
    if not isinstance(overview, dict) or not isinstance(segments, list) or not isinstance(needs, list) or not 0 < len(segments) <= MAX_SEGMENTS:
        raise ValueError("细分分析结构无效")

    def check_claim(claim, label, seasonal=False):
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip() or len(claim["text"]) > 3000:
            raise ValueError(f"{label}缺少有效文字")
        if claim.get("basis") not in {"observation", "synthesis", "inference"} or claim.get("classification") not in {"supported", "hypothesis"}:
            raise ValueError(f"{label}依据类型无效")
        refs = claim.get("evidence_refs")
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ValueError(f"{label}资料编号无效")
        if not refs and (claim["basis"] != "inference" or not str(claim.get("basis_note") or "").strip()):
            raise ValueError(f"{label}缺少资料或推断理由")
        if seasonal:
            if claim.get("kind") not in {"possible_use_period", "search_demand_peak", "sales_peak"}:
                raise ValueError("季节性结论类型无效")
            measured = claim.get("measured_months")
            if claim["kind"] in {"search_demand_peak", "sales_peak"} and (not isinstance(measured, list) or
                any(not isinstance(month, str) for month in measured) or len(set(measured)) < 3 or not refs):
                raise ValueError("实测趋势缺少三个可比月份或资料")

    def check_needs(items, label):
        if not isinstance(items, list) or len(items) > 50:
            raise ValueError(f"{label}格式无效")
        ids = set()
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or
                not item["id"].strip() or item["id"] in ids or
                not isinstance(item.get("text"), str) or not item["text"].strip() or len(item["text"]) > 1000):
                raise ValueError(f"{label}编号或文字无效")
            ids.add(item["id"])

    check_claim(overview, "当前样本综述")
    points = overview.get("points", [])
    if not isinstance(points, list) or len(points) > MAX_OVERVIEW_POINTS:
        raise ValueError("综述条目格式无效")
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("label"), str) or not point["label"].strip() or len(point["label"]) > 16:
            raise ValueError("综述条目标题无效")
        check_claim(point, "综述条目")
    ids = set()
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("id"), str) or not segment["id"].strip() or segment["id"] in ids or not isinstance(segment.get("name"), str) or not segment["name"].strip():
            raise ValueError("细分编号重复或名称无效")
        ids.add(segment["id"])
        fields = segment.get("fields")
        if not isinstance(fields, dict) or any(key not in fields for key in FIELDS):
            raise ValueError("细分缺少五项展示字段")
        for key in FIELDS:
            group = fields[key]
            if not isinstance(group, dict) or not isinstance(group.get("items"), list) or len(group["items"]) > MAX_ITEMS:
                raise ValueError(f"{FIELD_LABELS[key]}格式无效")
            if not group["items"] and not str(group.get("missing_reason") or "").strip():
                raise ValueError(f"{FIELD_LABELS[key]}缺少内容或原因")
            for claim in group["items"]:
                check_claim(claim, FIELD_LABELS[key], seasonal=key == "seasonality")
        check_needs(segment.get("needs_validation"), "细分待验证事项")
    check_needs(needs, "跨细分待验证事项")
    return value
