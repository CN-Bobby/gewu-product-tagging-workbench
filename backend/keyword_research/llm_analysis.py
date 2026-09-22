# -*- coding: utf-8 -*-
"""LLM 市场分析引擎（skill 步骤 11）：按 analysis-framework.md 拆 market/product/decision 三段。

容量设计（需求 3）：每段独立 prompt + JSON 输出；任一段失败或校验不过时，
以本地事实做保守确定性降级（analysis_mode=deterministic_fallback，产品方向为空、
进入等级固定 VALIDATE），并在分析元数据标记降级原因；不会因为模型故障阻断交付。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .llm_client import LLMClient, LLMError

OnEvent = Optional[Callable[[dict], None]]

SEGMENTS = ("demand", "quadrant", "product", "decision")

SYSTEM_PROMPT = """你是亚马逊市场关键词调研的资深分析师。基于给定的关键词词表、趋势数据与输入口径，
按字段契约输出严格的 JSON 对象。所有结论必须带数据证据（搜索量/购买率/集中度/PPC/趋势中的实际值），
并标记置信度 confidence（high/mid/low）。没有关键词证据的字段留空字符串或空数组，禁止编造。
禁止空泛表述（如"市场潜力巨大"）；一切结论用带数字的句子。"""


def _compact_keywords(data: dict) -> list[dict]:
    """压缩关键词词表为分析用精简行（控制 token 容量）。

    字段精简到 LLM 分析真正需要的 8 项；is_brand/is_noise 等过滤已由本地
    _local_analysis_stats 完成，不再进 prompt。
    """
    out = []
    for k in data.get("keywords", []):
        out.append({
            "keyword": k.get("keyword"),
            "searches": k.get("searches"),
            "purchase_rate": k.get("purchaseRate"),
            "relevance": k.get("relevance", ""),
            "traffic_type": k.get("traffic_type", ""),
            "bid": k.get("bid"),
        })
    return out


# 分析 prompt 携带的词表上限（按搜索量降序取 Top N，控制输入 token；
# 实测 deepseek-v4-flash 上下文总预算约 8-9K tokens，输入+输出超限会空输出/截断）
KEYWORDS_TOP_N = {"demand": 0, "quadrant": 60, "product": 40, "decision": 0}


def _median(values: list[float]) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    n = len(vals)
    if n % 2:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2


def _local_analysis_stats(labeled: dict, matrix: dict | None,
                          trend: dict | None) -> dict:
    """本地确定性预计算（skill analysis-framework §1/§3 口径），
    只把摘要给 LLM 补语义，避免把 matrix/trend 全文塞进上下文（实测 23 万字符超限）。"""
    keywords = labeled.get("keywords", [])
    # ---- 需求标签簇聚合 ----
    clusters: dict[str, dict] = {}
    for k in keywords:
        for tag in k.get("demand_tags") or []:
            if not isinstance(tag, str) or ":" not in tag:
                continue
            cl = clusters.setdefault(tag, {"kws": [], "searches": [],
                                           "purchase_rates": [], "ppc": []})
            cl["kws"].append(k)
            if k.get("searches") is not None:
                cl["searches"].append(float(k["searches"]))
            if k.get("purchaseRate") is not None:
                cl["purchase_rates"].append(float(k["purchaseRate"]))
            if k.get("bid") is not None:
                cl["ppc"].append(float(k["bid"]))
    demand_clusters = []
    for tag, cl in clusters.items():
        kws = sorted(cl["kws"], key=lambda x: x.get("searches") or 0, reverse=True)
        demand_clusters.append({
            "tag": tag,
            "kw_count": len(kws),
            "representative": [k.get("keyword") for k in kws[:3]],
            "searches_median": _median(cl["searches"]),
            "purchase_rate_median": _median(cl["purchase_rates"]),
            "ppc_median": _median(cl["ppc"]),
        })
    demand_clusters.sort(key=lambda x: x["kw_count"], reverse=True)
    demand_clusters = demand_clusters[:12]

    # ---- 点击-转化集中度象限候选（固定规则：relevance=A、非品牌非噪声、
    #      双指标齐全非全0、按月搜索量 Top 15）----
    pool = []
    for k in keywords:
        if str(k.get("relevance") or "").upper() != "A":
            continue
        if k.get("is_brand") or k.get("is_noise"):
            continue
        click = k.get("monopolyClickRate")
        conv = k.get("top3ConversionRate")
        if click is None or conv is None:
            continue
        try:
            click_f, conv_f = float(click), float(conv)
        except (TypeError, ValueError):
            continue
        if click_f == 0 and conv_f == 0:
            continue
        pool.append({"keyword": k.get("keyword"), "click": click_f,
                     "conv": conv_f, "searches": k.get("searches") or 0})
    pool.sort(key=lambda x: x["searches"], reverse=True)
    candidates = pool[:15]
    quadrant = {
        "click_median": _median([p["click"] for p in pool]),
        "conv_median": _median([p["conv"] for p in pool]),
        "candidates": [{"keyword": p["keyword"], "click": round(p["click"], 4),
                        "conv": round(p["conv"], 4)} for p in candidates],
    }

    # ---- 趋势摘要（每词一行，替代全文）----
    trend_summary: dict[str, str] = {}
    tkw = (trend or {}).get("keywords") or {}
    if isinstance(tkw, dict):
        for kw, info in tkw.items():
            stats = info.get("stats") or {}
            parts = []
            if stats.get("mom") is not None:
                parts.append(f"mom={stats['mom']:.2f}")
            if stats.get("yoy") is not None:
                parts.append(f"yoy={stats['yoy']:.2f}")
            if stats.get("latest_searches") is not None:
                parts.append(f"latest={stats['latest_searches']:.0f}")
            if parts:
                trend_summary[kw] = ", ".join(parts)

    return {
        "demand_clusters": demand_clusters,
        "quadrant": quadrant,
        "trend_summary": trend_summary,
    }


class LLMAnalyzer:
    def __init__(self, llm: LLMClient, on_event: OnEvent = None,
                 max_retries: int = 2) -> None:
        self.llm = llm
        self.on_event = on_event
        self.max_retries = max_retries

    def _emit(self, kind: str, **payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"kind": kind, **payload})
            except Exception:
                pass

    # ---------- 三段 prompt ----------

    def _segment_prompt(self, segment: str, ctx: dict) -> list[dict]:
        # 按段裁剪 payload：demand 段只用本地聚合结果，不携带完整词表
        local_stats = _local_analysis_stats(ctx["labeled"], ctx.get("matrix"),
                                            ctx.get("trend_analysis"))
        payload: dict = {
            "marketplace": ctx["input"].get("marketplace"),
            "mode": ctx["mode"],
            "data_month": (ctx["input"].get("options") or {}).get("data_month"),
            "local_stats": local_stats,
        }
        if segment != "demand":
            rows = _compact_keywords(ctx["labeled"])
            rows.sort(key=lambda x: x.get("searches") or 0, reverse=True)
            top_n = KEYWORDS_TOP_N.get(segment, 0)
            if top_n > 0:
                payload["keywords"] = rows[:top_n]
        if segment == "demand":
            instruction = """输出 JSON 对象，字段：
category_label(调研品类简短中文标签≤8字), demand_map(需求地图数组，元素:
  tag, kw_count, representative(3个代表关键词), searches_median, purchase_rate_median,
  click_conc_median, conv_conc_median, ppc_median, trend_direction(up/flat/down/seasonal),
  product_implication)
【确定性数据(不得修改)】local_stats.demand_clusters 已按需求标签簇聚合好统计值：
demand_map 必须直接使用这些行（tag/kw_count/representative/searches_median/
purchase_rate_median/click_conc_median/conv_conc_median/ppc_median 原样照抄），
只补写 product_implication(≤20字) 与 trend_direction(参考 local_stats.trend_summary，
没有对应趋势词时填 flat)。不得新增或删减簇。
【输出体积约束(重要)】demand_map 只输出 local_stats.demand_clusters 的**前 8 行**；
product_implication 必须 ≤20 字。宁可精简也不要超长输出。"""
        elif segment == "quadrant":
            instruction = """输出 JSON 对象，字段：
traffic_traps(高体量流量陷阱数组，元素: keyword, evidence),
quadrant(对象: click_median, conv_median, items(数组，元素: keyword, click, conv,
  quadrant(high-high/high-low/low-high/low-low), interpretation)),
opportunity_matrix(数组，元素: keyword, type(core/precise/longtail/differentiation/
  cost_pressure/monopoly/trap/declining/seasonal), evidence(对象，至少含 searches 与
  relevance 与 purchase_rate 之一), confidence(high/mid/low))
【确定性数据(不得修改)】local_stats.quadrant 已按固定规则选出双指标齐全的 Top 候选
并给出 click/conv 中位数：quadrant.items 必须使用这些候选(keyword/click/conv 原样)，
按候选值与中位数的比较判定 quadrant 值(高/低以 click_median/conv_median 为界)，
并补写一句 interpretation(≤15字)。quadrant.click_median/conv_median 照抄。
【输出体积约束(重要)】traffic_traps 最多 4 条；opportunity_matrix 最多 10 条；
interpretation 必须 ≤15 字；evidence 只保留 2 个数值字段。宁可精简也不要超长输出。"""
        elif segment == "product":
            instruction = """输出 JSON 对象，字段：
product_hypotheses(数组，元素固定字段:
  demand, source_keywords(必须来自关键词池的原始英文词), evidence(对象，含实际可得值
  searches/purchase_rate/ppc/concentration 至少一项), user_problem(结尾标注"（推断）"),
  product_form, material, function, spec, bundle_accessory, product_response(必须出现"假设"),
  confidence(high/mid/low), inference_level(固定"hypothesis"), validation_needed),
sku_structure(对象: main_form, bundle_demand, real_accessories, fake_accessories,
  sku_hypothesis(结尾标注"（假设）"))
约束: 产品形态/材料/功能/规格只能来自关键词池中实际出现的词及其标签；
没有关键词证据的字段留空字符串；评论痛点、供应链、销量利润只能写入 validation_needed。
【输出体积约束(重要)】product_hypotheses 最多 3 条（按证据强度优先）；
user_problem/product_response/validation_needed 各 ≤40 字。宁可精简也不要超长输出。"""
        else:  # decision
            instruction = """输出 JSON 对象，字段：
seasonality(对象: growth_start_month, peak_months(数组), peak_duration, off_duration,
  two_year_repeat(bool), timeline_advice),
lifecycle(对象: classification(growth/mature/seasonal/spike/migration/decline), evidence),
entry_conclusion(对象: grade(GO/CONDITIONAL/VALIDATE/HOLD), answers(对象 q1..q7 七个问题答案),
  evidence, limitations),
risk_checklist(对象: cannot_prove(数组), next_validations(数组))
7 个问题: ①需求是否真实稳定增长或季节性 ②竞争壁垒主要来自流量/成交/广告成本/产品形态
③最值得验证的产品方向 ④最适合切入的核心与长尾需求 ⑤现在是否值得继续调研
⑥何时开始产品和供应链动作(未给开发/生产/运输周期时只给相对提前量) ⑦哪些结论仍缺数据。
seasonality 的月份只能来自趋势数据实际出现的月份；无趋势数据时 growth_start_month 等
用空字符串并 two_year_repeat=false。"""
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)
             + "\n\n" + instruction},
        ]

    # ---------- 执行 ----------

    def analyze(self, workdir: Path) -> dict:
        """运行三段分析，合并写 cache/analysis.json，返回 analysis 字典。"""
        cache = Path(workdir) / "cache"
        input_data = json.loads((Path(workdir) / "input.json").read_text(encoding="utf-8"))
        labeled = json.loads((cache / "keyword_labeled.json").read_text(encoding="utf-8"))
        groups = input_data.get("groups") or []
        first = groups[0] if groups else {}
        ctx = {
            "input": {"marketplace": first.get("marketplace", ""),
                      "options": input_data.get("options", {})},
            "mode": _detect_mode(groups),
            "labeled": labeled,
            "trend_analysis": _load_json(cache / "trend_analysis.json"),
            "matrix": _load_json(cache / "asin_keyword_matrix.json"),
        }

        merged: dict = {"marketplace": first.get("marketplace", ""),
                        "mode": ctx["mode"]}
        # 段级续跑：恢复上次分析的全部数据字段，只重跑失败/缺失的段
        prev = _load_json(cache / "analysis.json") or {}
        prev_sections = prev.get("analysis_sections") or {}
        meta_keys = {"analysis_sections", "analysis_mode", "degraded_reasons",
                     "marketplace", "mode"}
        for k, v in prev.items():
            if k not in meta_keys:
                merged[k] = v
        # 执行段（4 段，容量稳定）；元数据按 skill 三段契约（market=demand+quadrant）记录
        exec_segments = ("demand", "quadrant", "product", "decision")
        part_status: dict[str, str] = {}
        part_errors: dict[str, str] = {}
        for segment in exec_segments:
            # prev_sections 是三段结构：demand/quadrant 状态在 market.parts 里
            if segment in ("demand", "quadrant"):
                mkt = prev_sections.get("market") or {}
                cached_ok = (mkt.get("parts") or {}).get(segment) == "ok"
            else:
                cached_ok = prev_sections.get(segment, {}).get("sub_status") == "ok"
            if cached_ok:
                part_status[segment] = "ok"
                self._emit("analysis_segment_cached", segment=segment)
                continue
            self._emit("analysis_segment_start", segment=segment)
            try:
                part = self._run_segment(segment, ctx)
                merged.update(part)
                part_status[segment] = "ok"
            except Exception as exc:  # noqa: BLE001
                part_status[segment] = "degraded"
                part_errors[segment] = str(exc)[:200]
                self._emit("analysis_segment_degraded", segment=segment,
                           error=str(exc)[:200])

        # 三段元数据（skill 契约）：market 由 demand+quadrant 合并
        def _status(*segs: str) -> str:
            return "ok" if all(part_status.get(s) == "ok" for s in segs) else "degraded"

        sections = {
            "market": {
                "status": _status("demand", "quadrant"),
                "sub_status": _status("demand", "quadrant"),
                "parts": {s: part_status.get(s, "degraded") for s in ("demand", "quadrant")},
            },
            "product": {"status": _status("product"), "sub_status": _status("product")},
            "decision": {"status": _status("decision"), "sub_status": _status("decision")},
        }
        degraded: list[str] = []
        for seg, err in part_errors.items():
            degraded.append(f"{seg}: {err}")
        ok_count = sum(1 for s in ("market", "product", "decision")
                       if sections[s]["status"] == "ok")

        merged["analysis_sections"] = sections
        # 三段契约（skill v1.6.2）：market/product/decision
        merged["analysis_mode"] = (
            "model_split" if ok_count == 3
            else ("hybrid_fallback" if ok_count > 0 else "deterministic_fallback")
        )
        merged["degraded_reasons"] = degraded
        # 保守确定性降级：补全必填结构，避免 Excel 生成崩溃
        merged.setdefault("category_label", _fallback_category(labeled))
        merged.setdefault("demand_map", [])
        merged.setdefault("traffic_traps", [])
        merged.setdefault("quadrant", {"click_median": None, "conv_median": None, "items": []})
        merged.setdefault("opportunity_matrix", [])
        merged.setdefault("product_hypotheses", [])
        merged.setdefault("sku_structure", {})
        merged.setdefault("seasonality", {})
        merged.setdefault("lifecycle", {})
        merged.setdefault("entry_conclusion", _fallback_entry(merged.get("marketplace", "")))
        merged.setdefault("risk_checklist", {"cannot_prove": [], "next_validations": []})
        _normalize_analysis(merged)

        out_path = cache / "analysis.json"
        out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return merged

    def _run_segment(self, segment: str, ctx: dict) -> dict:
        messages = self._segment_prompt(segment, ctx)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = self.llm.complete(messages, json_mode=True)
                if result.truncated:
                    raise LLMError("输出截断（finish_reason=length）", retryable=True)
                part = result.json()
                if not isinstance(part, dict):
                    raise ValueError("分析段输出不是 JSON 对象")
                return part
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._emit("analysis_segment_retry", segment=segment,
                           attempt=attempt + 1, error=str(exc)[:200])
        raise RuntimeError(f"分析段 {segment} 失败: {last_error}") from last_error


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def segment_data_key(segment: str) -> str:
    """analysis.json 中各段的数据字段名（对应段的主键字段）。"""
    mapping = {
        "demand": "demand_map",
        "quadrant": "quadrant",
        "product": "product_hypotheses",
        "decision": "entry_conclusion",
    }
    return mapping.get(segment, segment)


def _detect_mode(groups: list[dict]) -> str:
    """M1=单ASIN+Node / M2=多ASIN / M3=纯Node / M4=纯单ASIN。"""
    if not groups:
        return "M3"
    g = groups[0]
    a = len(g.get("asins") or [])
    n = len(g.get("node_ids") or [])
    if a == 1 and n >= 1:
        return "M1"
    if n >= 1 and a == 0:
        return "M3"
    if a == 1:
        return "M4"
    return "M2"


def _fallback_category(labeled: dict) -> str:
    """确定性降级：用最高频词族作品类标签。"""
    fams: dict[str, int] = {}
    for k in labeled.get("keywords", []):
        f = k.get("family") or ""
        if f:
            fams[f] = fams.get(f, 0) + 1
    if fams:
        return max(fams, key=fams.get)[:8]
    return "关键词调研"


def _fallback_entry(marketplace: str) -> dict:
    return {
        "grade": "VALIDATE",
        "answers": {
            "q1": "模型分析降级，需人工复核数据。",
            "q2": "模型分析降级，需人工复核数据。",
            "q3": "模型分析降级，需人工复核数据。",
            "q4": "模型分析降级，需人工复核数据。",
            "q5": "模型分析降级，需人工复核数据。",
            "q6": "模型分析降级，需人工复核数据。",
            "q7": "模型分析降级，需人工复核数据。",
        },
        "evidence": "本地确定性降级：模型分析未通过",
        "limitations": "模型分析失败，本报告为数据事实汇总，市场结论需人工复核。",
    }


_STRING_FIELDS = {
    "entry_conclusion": ("evidence", "limitations"),
    "lifecycle": ("evidence",),
    "seasonality": ("growth_start_month", "peak_duration", "off_duration",
                    "timeline_advice"),
    "demand_map": ("product_implication", "trend_direction"),
    "quadrant_items": ("interpretation",),
    "product_hypotheses": ("demand", "user_problem", "product_form", "material",
                           "function", "spec", "bundle_accessory",
                           "product_response", "validation_needed"),
}


def _normalize_analysis(merged: dict) -> dict:
    """schema 容错：真实 LLM 输出偶发把字符串字段写成数组，统一规整为字符串，
    避免 build_excel_report 因单元格收到 list 崩溃（skill 脚本不做此容错）。"""
    for section, fields in _STRING_FIELDS.items():
        if section == "quadrant_items":
            for item in (merged.get("quadrant") or {}).get("items", []) or []:
                for fld in fields:
                    _stringify(item, fld)
        elif section == "demand_map":
            for row in merged.get("demand_map", []) or []:
                for fld in fields:
                    _stringify(row, fld)
        elif section == "product_hypotheses":
            for h in merged.get("product_hypotheses", []) or []:
                for fld in fields:
                    _stringify(h, fld)
                ev = h.get("evidence")
                if isinstance(ev, list):
                    h["evidence"] = {"evidence": "；".join(
                        str(x) for x in ev if x)}
                elif isinstance(ev, str):
                    # 真实 LLM 偶发把 evidence 输出为字符串（实测）
                    h["evidence"] = {"evidence": ev}
        elif section == "entry_conclusion":
            obj = merged.get("entry_conclusion") or {}
            for fld in fields:
                _stringify(obj, fld)
        else:
            obj = merged.get(section) or {}
            for fld in fields:
                _stringify(obj, fld)
    # opportunity_matrix：evidence 必须是对象；列表/字符串时包一层
    for row in merged.get("opportunity_matrix", []) or []:
        if isinstance(row.get("evidence"), list):
            row["evidence"] = {"evidence": "；".join(
                str(x) for x in row["evidence"] if x)}
        elif isinstance(row.get("evidence"), str):
            row["evidence"] = {"evidence": row["evidence"]}
    return merged


def _stringify(obj: dict, field: str) -> None:
    v = obj.get(field)
    if isinstance(v, list):
        obj[field] = "；".join(str(x) for x in v if x)
