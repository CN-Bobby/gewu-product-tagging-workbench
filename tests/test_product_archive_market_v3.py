"""Offline contracts for concise market-segment cards."""

from __future__ import annotations

import copy

import pytest

from backend.product_archive import market_analysis_v3, overview_drafts, reasonix_research, service


def claim(text, *, basis="inference", refs=None, kind=None, months=None):
    result = {"text": text, "classification": "supported" if refs else "hypothesis",
              "basis": basis, "basis_note": "根据商品用途推断，仍需实际需求验证。",
              "evidence_refs": refs or [], "limits": "仅适用于当前样本。"}
    if kind:
        result.update(kind=kind, measured_months=months or [])
    return result


def generated():
    return {"market_analysis": {"schema_version": 3,
        "overview": claim("当前样本包含作业与日常两种需求线索。", basis="synthesis", refs=["artifact:table"]),
        "segments": [{"id": "S1", "name": "蓝领技工作业裤", "fields": {
            "product": {"items": [claim("宽松弹力斜纹 cargo 供作业者蹲跪与携物；不适合正式商务场合。", basis="synthesis", refs=["artifact:table"])], "missing_reason": ""},
            "audience": {"items": [claim("建筑与仓储作业者；家人可能代购。", basis="observation", refs=["artifact:reviews"])], "missing_reason": ""},
            "scenarios": {"items": [claim("工作班次中蹲跪、布线并随身携带工具。", basis="observation", refs=["artifact:reviews"])], "missing_reason": ""},
            "culture": {"items": [], "missing_reason": "目前未发现足以判断的特定文化因素。"},
            "seasonality": {"items": [claim("推断全年有基础需求，地区气候影响面料选择。", kind="possible_use_period")], "missing_reason": ""},
        }, "needs_validation": [{"id": "V1", "text": "需要跨月需求数据确认销售旺季。"}]}],
        "needs_validation": []}}


EVIDENCE = {"artifact:table": {"source_type": "tagged_workbook"},
            "artifact:reviews": {"source_type": "reviews"}}


def test_v3_normalization_keeps_five_fields_and_no_business_keywords():
    suggestions, diagnostics = overview_drafts._build_analysis(
        "run", generated(), EVIDENCE, [], "男士休闲裤", "2026-10", [])
    assert suggestions == {}
    assert diagnostics["completion_blockers"] == []
    result = diagnostics["market_analysis"]
    assert result["schema_version"] == 3
    assert list(result["segments"][0]["fields"]) == list(market_analysis_v3.FIELDS)
    assert result["segments"][0]["fields"]["culture"]["missing_reason"]
    assert "workflow_keywords" not in str(result)
    assert service._validate_market_analysis(result) == result
    assert reasonix_research._expected_repair_shape(generated())


def test_overview_points_keep_separate_evidence_and_old_overview_still_validates():
    raw = generated()["market_analysis"]
    raw["overview"]["points"] = [
        {**claim("100 条 US 男士裤子样本。", basis="observation", refs=["artifact:table"]),
         "label": "样本范围"},
        {**claim("这一价格判断缺少有效资料。", basis="observation", refs=["unknown"]),
         "label": "价格位置"},
    ]
    _, diagnostics = market_analysis_v3.normalize(raw, EVIDENCE)
    points = diagnostics["market_analysis"]["overview"]["points"]
    assert [point["label"] for point in points] == ["样本范围"]
    assert points[0]["evidence_refs"] == ["artifact:table"]
    assert any(item["field"].startswith("overview.points[2]") for item in diagnostics["filtered"])
    assert service._validate_market_analysis(diagnostics["market_analysis"])
    assert service._validate_market_analysis(market_analysis_v3.normalize(
        generated()["market_analysis"], EVIDENCE)[1]["market_analysis"])


def test_invalid_source_filters_one_claim_without_erasing_other_fields():
    raw = generated()["market_analysis"]
    raw["segments"][0]["fields"]["audience"]["items"][0]["evidence_refs"] = ["made-up"]
    _, diagnostics = market_analysis_v3.normalize(raw, EVIDENCE)
    segment = diagnostics["market_analysis"]["segments"][0]
    assert segment["fields"]["audience"]["items"] == []
    assert segment["fields"]["audience"]["missing_reason"]
    assert segment["fields"]["product"]["items"]
    assert diagnostics["filtered"]
    assert not diagnostics["completion_blockers"]


def test_only_keywords_or_empty_fields_cannot_become_ready_analysis():
    raw = generated()["market_analysis"]
    for field in market_analysis_v3.FIELDS:
        raw["segments"][0]["fields"][field] = {"items": [], "missing_reason": "暂无资料"}
    _, diagnostics = market_analysis_v3.normalize(raw, EVIDENCE)
    assert diagnostics["completion_blockers"]


def test_possible_use_period_survives_one_month_but_unproven_sales_peak_does_not():
    raw = generated()["market_analysis"]
    seasonal = raw["segments"][0]["fields"]["seasonality"]["items"]
    seasonal.append(claim("10 月是实际销售旺季。", kind="sales_peak", basis="synthesis",
                          refs=["artifact:table"], months=["2026-10"]))
    _, diagnostics = market_analysis_v3.normalize(raw, EVIDENCE, ["2026-10"])
    result = diagnostics["market_analysis"]["segments"][0]["fields"]["seasonality"]["items"]
    assert len(result) == 1 and result[0]["kind"] == "possible_use_period"
    assert any("实测销量旺季" in item["reason"] for item in diagnostics["filtered"])


def test_distinct_product_and_task_cards_keep_separate_ids_and_content():
    raw = generated()["market_analysis"]
    second = copy.deepcopy(raw["segments"][0])
    second["id"] = "S2"
    second["name"] = "通勤卡其裤"
    second["fields"]["product"]["items"][0]["text"] = "直筒弹力斜纹裤供通勤者在办公室与周末穿着；不适合作业防护。"
    second["fields"]["audience"]["items"][0]["text"] = "需要非正式办公着装的通勤者。"
    second["fields"]["scenarios"]["items"][0]["text"] = "工作日通勤和非正式办公。"
    raw["segments"].append(second)
    _, diagnostics = market_analysis_v3.normalize(raw, EVIDENCE)
    cards = diagnostics["market_analysis"]["segments"]
    assert [card["id"] for card in cards] == ["S1", "S2"]
    assert cards[0]["fields"]["product"]["items"][0]["text"] != cards[1]["fields"]["product"]["items"][0]["text"]
    assert cards[0]["fields"]["scenarios"]["items"][0]["text"] != cards[1]["fields"]["scenarios"]["items"][0]["text"]


def test_search_demand_peak_requires_matching_month_sources():
    raw = generated()["market_analysis"]
    raw["segments"][0]["fields"]["seasonality"]["items"] = [claim(
        "三个可比月份中 6 月的搜索需求最高。", kind="search_demand_peak", basis="synthesis",
        refs=["month:04", "month:05", "month:06"], months=["2026-04", "2026-05", "2026-06"])]
    sources = {**EVIDENCE, **{f"month:{month[-2:]}": {"source_type": "keyword_research", "data_month": month}
                            for month in ("2026-04", "2026-05", "2026-06")}}
    _, diagnostics = market_analysis_v3.normalize(raw, sources, ["2026-04", "2026-05", "2026-06"])
    assert diagnostics["market_analysis"]["segments"][0]["fields"]["seasonality"]["items"][0]["kind"] == "search_demand_peak"


def test_search_peak_can_explicitly_disclaim_sales_peak_without_being_discarded():
    raw = generated()["market_analysis"]
    raw["segments"][0]["fields"]["seasonality"]["items"] = [claim(
        "6 月搜索需求较高；这不等于商品销量峰值。", kind="search_demand_peak",
        basis="synthesis", refs=["artifact:keyword-report"],
        months=["2025-05", "2025-06", "2025-07"])]
    evidence = {**EVIDENCE, "artifact:keyword-report": {
        "source_type": "keywords", "metric": "monthly_searches",
        "data_months": ["2025-05", "2025-06", "2025-07"]}}
    _, diagnostics = market_analysis_v3.normalize(raw, evidence,
        ["2025-05", "2025-06", "2025-07"])
    items = diagnostics["market_analysis"]["segments"][0]["fields"]["seasonality"]["items"]
    assert len(items) == 1 and items[0]["kind"] == "search_demand_peak"

    raw["segments"][0]["fields"]["seasonality"]["items"][0]["text"] = "6 月搜索高，因此商品销量峰值在 6 月。"
    _, diagnostics = market_analysis_v3.normalize(raw, evidence,
        ["2025-05", "2025-06", "2025-07"])
    assert not diagnostics["market_analysis"]["segments"][0]["fields"]["seasonality"]["items"]


def test_keyword_search_high_point_cannot_be_labeled_sales_peak():
    raw = generated()["market_analysis"]
    raw["segments"][0]["fields"]["seasonality"]["items"] = [claim(
        "10 月是实际销售旺季。", kind="sales_peak", basis="synthesis",
        refs=["artifact:keyword-report"], months=["2025-09", "2025-10", "2025-11"])]
    sources = {**EVIDENCE, "artifact:keyword-report": {
        "source_type": "keywords", "metric": "monthly_searches",
        "data_months": ["2025-09", "2025-10", "2025-11"]}}
    _, diagnostics = market_analysis_v3.normalize(raw, sources, ["2025-09", "2025-10", "2025-11"])
    assert diagnostics["market_analysis"]["segments"][0]["fields"]["seasonality"]["items"] == []
    assert any("实测销量旺季" in item["reason"] for item in diagnostics["filtered"])


def test_save_validation_preserves_v2_contract_and_rejects_invalid_v3():
    _, diagnostics = market_analysis_v3.normalize(generated()["market_analysis"], EVIDENCE)
    result = diagnostics["market_analysis"]
    broken = copy.deepcopy(result)
    broken["segments"][0]["fields"]["product"]["items"][0]["basis"] = "fabricated"
    with pytest.raises(ValueError, match="依据类型无效"):
        service._validate_market_analysis(broken)
    assert service._validate_market_analysis(result)["schema_version"] == 3
