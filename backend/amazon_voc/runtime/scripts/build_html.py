# -*- coding: utf-8 -*-
"""Render the V4 six-module frozen HTML from cache/analysis.json."""
import argparse
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from compile_analysis import compile_analysis


def _sanitize_source_names(value, replacement):
    text = str(value or "")
    text = re.sub(r"Alexa\s*/\s*Customer\s+Say", replacement, text, flags=re.I)
    text = re.sub(r"\bAlexa\b|Customer\s+Say", replacement, text, flags=re.I)
    return text


def esc(value, replacement="other sources"):
    return html.escape(_sanitize_source_names(value, replacement), quote=True)


def labels(items, empty="本次样本未形成稳定信号"):
    values = []
    for item in items or []:
        if isinstance(item, dict):
            label = item.get("label") or item.get("need") or item.get("name") or ""
            count = item.get("review_count")
            values.append("%s%s" % (label, "（%s）" % count if count not in (None, "") else ""))
        else:
            values.append(str(item))
    return "、".join(esc(v) for v in values if v) or '<span class="empty">%s</span>' % esc(empty)


def evidence_links(items):
    links = []
    for item in items or []:
        if item.get("quote_omitted"):
            quote = "完整评论未嵌入HTML，请按事件ID在Excel证据索引中查看。"
            label = "%s（原文见Excel）" % item.get("event_id", "")
        else:
            quote = item.get("quote") or "无片段"
            if len(quote) > 39:
                quote = quote[:38].rstrip() + "…"
            label = item.get("event_id", "")
        links.append('<button class="evidence-link" data-quote="%s">%s</button>' %
                     (esc(quote), esc(label)))
    return " ".join(links) or "无"


def mini_table(headers, rows):
    if not rows:
        return '<p class="empty">本次样本未观察到。</p>'
    return '<div class="table-wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        "".join("<th>%s</th>" % esc(v) for v in headers),
        "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % value for value in row) for row in rows))


def bilingual(zh, en, empty="未观察到直接证据 / No direct evidence observed"):
    zh, en = str(zh or "").strip(), str(en or "").strip()
    if not zh and not en:
        return '<span class="empty">%s</span>' % esc(empty)
    return '<span class="dual-pair"><span class="dual-zh">%s</span><small lang="en">%s</small></span>' % (
        esc(zh or empty.split(" / ", 1)[0], "其他未提供的数据源"),
        esc(en or empty.split(" / ", 1)[-1], "other unprovided sources"))


SYNTHESIS_LABELS = {
    "users": ("使用者", "Users"), "buyers": ("购买者", "Buyers"),
    "relationships": ("使用与购买关系", "User–buyer relationship"),
    "physical_constraints": ("身体与物理限制", "Physical constraints"),
    "skill_levels": ("使用熟练度", "Skill level"), "role_needs": ("角色诉求", "Role needs"),
    "scenario": ("场景", "Scenario"), "motivation": ("动机", "Motivation"),
    "task": ("核心任务", "Core task"), "pains": ("痛点", "Pain points"),
    "itches": ("痒点", "Experience improvements"),
    "desired_outcomes": ("期望结果", "Desired outcomes"),
    "product_experience": ("产品体验主题", "Product experience"),
    "workarounds": ("用户自救方式", "Workarounds"),
}


def synthesis_cards(segment):
    synthesis = segment.get("profile_synthesis") or {}
    cards = []
    for key, labels_pair in SYNTHESIS_LABELS.items():
        if key not in synthesis:
            continue
        value = synthesis.get(key) or {}
        cards.append('<article class="synthesis-card"><h5>%s <small>%s</small></h5><p>%s</p></article>' % (
            esc(labels_pair[0]), esc(labels_pair[1]), bilingual(value.get("zh"), value.get("en"))))
    return '<div class="synthesis-grid">%s</div>' % "".join(cards)


def segment_overview_card(segment):
    key, key_en = segment.get("segment_key") or {}, segment.get("segment_key_en") or {}
    metrics = segment.get("metrics") or {}
    return '''<article class="overview-card">
<div class="overview-card-head"><span class="pill">{status}</span><span class="coverage">{coverage:.1f}%</span></div>
<h3>{name}</h3><p class="english-title" lang="en">{name_en}</p>
<div class="overview-facets"><div><b>人群 · Audience</b>{audience}</div><div><b>场景 · Scenario</b>{scenario}</div>
<div><b>动机 · Motivation</b>{motivation}</div><div><b>任务 · Task</b>{task}</div></div>
<p class="overview-jtbd">{jtbd}</p><small>{reviews} 条涉及评论 / reviews · {primary_reviews} 条主归属评论 / primary reviews · {events} 个事件 / events · {asins} 个 ASIN</small></article>'''.format(
        status="正式细分 / Formal" if segment.get("status") == "formal" else "探索信号 / Exploratory",
        coverage=float(metrics.get("review_coverage", 0))*100,
        name=esc(segment.get("name")), name_en=esc(segment.get("name_en")),
        audience=bilingual(key.get("audience"), key_en.get("audience")),
        scenario=bilingual(key.get("scenario"), key_en.get("scenario")),
        motivation=bilingual(key.get("motivation"), key_en.get("motivation")),
        task=bilingual(key.get("task"), key_en.get("task")),
        jtbd=bilingual(segment.get("jtbd_statement"), segment.get("jtbd_statement_en")),
        reviews=metrics.get("review_count", 0), events=metrics.get("event_count", 0),
        primary_reviews=metrics.get("primary_review_count", 0),
        asins=metrics.get("asin_count", 0))


def comparison_card(segment):
    synthesis = segment.get("profile_synthesis") or {}
    def pair(field):
        value = synthesis.get(field) or {}
        return bilingual(value.get("zh"), value.get("en"))
    return '''<article class="comparison-card" data-compare-row="{sid}">
<header><label><input class="compare" type="checkbox" checked> <span class="pill">{status}</span></label>
<div><h3>{name}</h3><small lang="en">{name_en}</small></div><b>{coverage:.1f}%</b></header>
<div class="comparison-facets"><div><b>人群 / Audience</b>{audience}</div><div><b>任务 / Task</b>{task}</div>
<div><b>动机 / Motivation</b>{motivation}</div><div><b>痛点 / Pains</b>{pains}</div>
<div><b>痒点 / Itches</b>{itches}</div><div><b>期望 / Outcomes</b>{outcomes}</div></div></article>'''.format(
        sid=esc(segment.get("segment_id")), status="正式 / Formal" if segment.get("status") == "formal" else "探索 / Exploratory",
        name=esc(segment.get("name")), name_en=esc(segment.get("name_en")),
        coverage=float((segment.get("metrics") or {}).get("review_coverage", 0))*100,
        audience=pair("users"), task=pair("task"), motivation=pair("motivation"),
        pains=pair("pains"), itches=pair("itches"), outcomes=pair("desired_outcomes"))


def journey_stage_cards(segments, stages, cells):
    lookup = {(row.get("segment_id"), row.get("stage_id")): row for row in cells}
    cards = []
    for stage in stages:
        rows = []
        for segment in segments:
            cell = lookup.get((segment.get("segment_id"), stage.get("stage_id")), {})
            observed = cell.get("status") == "observed"
            rows.append('''<article class="journey-cell {state}"><h4>{segment}</h4><small lang="en">{segment_en}</small>
<div class="cell-metric">{count} <span>评论 / reviews</span> · {status}</div>
<p><b>关键动作 / Actions</b><br>{actions}</p><p><b>目标 / Goals</b><br>{goals}</p></article>'''.format(
                state="observed" if observed else "not-observed", segment=esc(segment.get("name")),
                segment_en=esc(segment.get("name_en")), count=esc(cell.get("review_count", 0)),
                status="有证据 / Observed" if observed else "未观察到 / Not observed",
                actions=labels(cell.get("actions"), "未观察到 / Not observed"),
                goals=labels(cell.get("goals"), "未观察到 / Not observed")))
        need, friction = stage.get("user_need") or {}, stage.get("friction_or_outcome") or {}
        stage_detail = ""
        if need or friction:
            stage_detail = '<div class="journey-insight-pairs"><p><b>用户需要 / User need</b>{need}</p><p><b>摩擦或结果 / Friction or outcome</b>{friction}</p></div>'.format(
                need=bilingual(need.get("zh"), need.get("en")),
                friction=bilingual(friction.get("zh"), friction.get("en")))
        cards.append('''<article class="journey-stage-card"><header><span class="stage-order">{order:02d}</span>
<div><h3>{name}</h3><p lang="en">{name_en}</p></div></header><p class="stage-definition">{definition}</p>
<p class="stage-definition" lang="en">{definition_en}</p>{stage_detail}<div class="journey-cell-grid">{rows}</div></article>'''.format(
            order=int(stage.get("order", 0)), name=esc(stage.get("name")), name_en=esc(stage.get("name_en")),
            definition=esc(stage.get("definition")), definition_en=esc(stage.get("definition_en")),
            stage_detail=stage_detail, rows="".join(rows)))
    return "".join(cards) or '<p class="empty">本次样本未形成旅程阶段 / No journey stages were observed.</p>'


def segment_card(segment, cells, stages):
    key, key_en = segment.get("segment_key") or {}, segment.get("segment_key_en") or {}
    metrics = segment.get("metrics") or {}
    workaround_rows = [[esc(w.get("standardized_action") or w.get("raw_action")), esc(w.get("strategy")),
                        esc("、".join(w.get("costs") or [])), esc(w.get("effect"))]
                       for w in (segment.get("workarounds") or [])[:12]]
    priority_rows = [[esc(row.get("need")), esc(row.get("priority_type")), esc(row.get("basis")), esc(row.get("confidence"))]
                     for row in segment.get("need_priorities") or []]
    priority_section = ('<h4>类 KANO 推断</h4><p class="boundary">仅按评论证据组织优先级，不是正式 KANO 问卷结论。</p>%s' %
                        mini_table(["需求", "类型", "依据", "置信度"], priority_rows)) if priority_rows else ""
    evidence_trace = [
        ("使用者 / Users", segment.get("audience_details", {}).get("users")),
        ("购买者 / Buyers", segment.get("audience_details", {}).get("buyers")),
        ("关系 / Relationships", segment.get("audience_details", {}).get("relationships")),
        ("物理限制 / Physical constraints", segment.get("audience_details", {}).get("physical_constraints")),
        ("角色诉求 / Role needs", segment.get("audience_details", {}).get("role_needs")),
        ("痛点 / Pain points", segment.get("pains")), ("痒点 / Itches", segment.get("itches")),
        ("期望 / Desired outcomes", segment.get("desired_outcomes")),
    ]
    trace = "".join('<div class="trace-row"><b>%s</b><span>%s</span></div>' %
                     (esc(label), labels(values)) for label, values in evidence_trace)
    segment_journey = journey_stage_cards([segment], stages, cells)
    return '''<details class="segment-card" data-segment-id="{sid}" data-status="{raw_status}" data-search="{search}">
<summary><div><span class="pill">{status}</span><h3>{name}</h3><div class="english-title" lang="en">{name_en}</div><div class="dims">{dims}</div></div>
<div class="metric"><b>{rate:.1f}%</b><span>{reviews}条涉及评论 / {primary_reviews}条主归属 / {events}事件 / {asins} ASIN</span></div></summary>
<div class="card-body"><div class="headline-synthesis"><div><b>核心 JTBD / Core job</b>{jtbd}</div><div><b>综合画像 / Persona synthesis</b>{persona}</div></div>
<h4>用户综合洞察 / User synthesis</h4><p class="boundary">模型把同维度、相近含义的评论线索归纳成主题；底层计数可展开回查。</p>{synthesis}
{priority_section}
<details class="trace-details"><summary>查看底层原始表达计数 / Inspect atomic evidence counts</summary><p class="boundary">以下仅用于追溯评论中的字段表达与计数，不是最终洞察结论。最终结论见上方模型归纳。</p><div class="trace-grid">{trace}</div>
<h4>Workaround 证据明细 / Evidence details</h4>{workarounds}</details>
<h4>本细分旅程差异 / Segment journey</h4>{journey}
<h4>证据回查</h4><p>{evidence}</p></div></details>'''.format(
        sid=esc(segment.get("segment_id")), raw_status=esc(segment.get("status")),
        status="正式 / Formal" if segment.get("status") == "formal" else "探索 / Exploratory",
        search=esc(" ".join(str(v) for v in [segment.get("name"), segment.get("name_en"), *key.values(), *key_en.values()])),
        name=esc(segment.get("name")), name_en=esc(segment.get("name_en")),
        dims=" · ".join(filter(None, (bilingual(key.get("audience"), key_en.get("audience")),
                                       bilingual(key.get("scenario"), key_en.get("scenario")),
                                       bilingual(key.get("motivation"), key_en.get("motivation")),
                                       bilingual(key.get("task"), key_en.get("task"))))),
        rate=float(metrics.get("review_coverage", 0))*100,
        reviews=metrics.get("review_count", 0), primary_reviews=metrics.get("primary_review_count", 0),
        events=metrics.get("event_count", 0), asins=metrics.get("asin_count", 0),
        jtbd=bilingual(segment.get("jtbd_statement"), segment.get("jtbd_statement_en")),
        persona=bilingual(segment.get("persona_summary"), segment.get("persona_summary_en")),
        synthesis=synthesis_cards(segment), trace=trace,
        priority_section=priority_section,
        workarounds=mini_table(["动作 / Action", "策略 / Strategy", "成本 / Cost", "效果 / Effect"], workaround_rows),
        journey=segment_journey,
        evidence=evidence_links(segment.get("evidence_snippets")))


def v2_insight_cards(items):
    items = items or []
    if not items:
        return '<p class="empty">本次样本未形成稳定信号 / No stable signal in this sample.</p>'
    cards = []
    for index, item in enumerate(items, 1):
        item = item if isinstance(item, dict) else {}
        cards.append('<article class="v2-insight-card"><span class="insight-index">%02d</span>%s</article>' %
                     (index, bilingual(item.get("zh"), item.get("en"))))
    return '<div class="v2-insight-grid">%s</div>' % "".join(cards)


def build_html(workdir):
    path = common.cache_path(workdir, "analysis.json")
    data = common.read_json(path) if os.path.exists(path) else compile_analysis(workdir)
    task, method = data.get("task") or {}, data.get("method") or {}
    segments = data.get("segments") or []
    formal = [s for s in segments if s.get("status") == "formal"]
    exploratory = [s for s in segments if s.get("status") != "formal"]
    journey = data.get("journey") or {}; stages, cells = journey.get("stages") or [], journey.get("cells") or []
    counts = data.get("counts") or {"reviews": task.get("review_count", 0), "events": 0, "segments": len(segments)}
    dictionary_summary = data.get("dictionary_summary") or {}
    quality = data.get("quality") or {}
    gate_rows = [[esc(g.get("name")), "通过" if g.get("pass") else "失败", esc(g.get("failed_count", 0))]
                 for g in quality.get("gates") or []]
    overview_cards = "".join(segment_overview_card(s) for s in segments)
    comparison_cards = "".join(comparison_card(s) for s in segments)
    journey_cards = journey_stage_cards(segments, stages, cells)
    html_text = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>
:root{{--ink:#172b32;--muted:#63747a;--line:#d9e2df;--brand:#087f8c;--soft:#f2f6f4;--warm:#fbf6eb;--warn:#9b4c20;--shadow:0 12px 30px #16373a0d}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth;scroll-padding-top:24px}}body{{margin:0;background:#e9efec;color:var(--ink);font:14px/1.72 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1320px;margin:auto;padding:34px 24px 84px}}header,.module{{background:#fff;border:1px solid var(--line);border-radius:18px;padding:24px;margin-bottom:22px;box-shadow:var(--shadow)}}h1,h2,h3,h4,h5{{line-height:1.32}}h1{{font-size:clamp(27px,4vw,40px);letter-spacing:-.03em}}h1,h2{{margin-top:0}}.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}}.stats div{{background:var(--soft);border-radius:12px;padding:14px}}.stats b{{display:block;font-size:22px}}.toolbar{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}}input,button{{padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:white;color:var(--ink)}}button{{cursor:pointer}}button:hover{{border-color:var(--brand);color:var(--brand)}}.segment-card,.overview-card,.comparison-card,.journey-stage-card{{border:1px solid var(--line);border-radius:15px;margin:12px 0;background:#fff;box-shadow:var(--shadow)}}.segment-card{{overflow:hidden}}summary{{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:18px;cursor:pointer;list-style:none}}summary::-webkit-details-marker{{display:none}}summary h3{{display:inline;margin:0 8px;font-size:20px}}.card-body{{border-top:1px solid var(--line);padding:20px}}.pill{{display:inline-flex;background:#e4f4f2;color:#09636b;padding:4px 10px;border-radius:99px;font-size:12px;font-weight:700}}.metric{{text-align:right;white-space:nowrap}}.metric b,.metric span{{display:block}}.metric b{{font-size:25px;color:var(--brand)}}.dims,.empty,.boundary,small{{color:var(--muted)}}.english-title{{color:#537079;margin:4px 0;font-size:14px}}.dual-pair{{display:flex;flex-direction:column;gap:3px}}.dual-zh{{font-weight:600}}.dual-pair small{{font-size:12px;line-height:1.45}}.overview-grid,.comparison-grid,.journey-stage-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.overview-card,.comparison-card,.journey-stage-card{{padding:18px;margin:0;transition:transform .16s ease,border-color .16s ease}}.overview-card:hover,.comparison-card:hover,.journey-stage-card:hover{{transform:translateY(-2px);border-color:#8bc4c2}}.overview-card-head{{display:flex;align-items:center;justify-content:space-between}}.coverage{{font-size:24px;font-weight:750;color:var(--brand)}}.overview-card h3{{margin:14px 0 2px;font-size:21px}}.overview-facets,.comparison-facets{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:14px 0}}.overview-facets>div,.comparison-facets>div{{background:var(--soft);border-radius:11px;padding:11px;min-width:0}}.overview-facets b,.comparison-facets b{{display:block;font-size:12px;color:#537079;margin-bottom:5px}}.overview-jtbd{{border-left:3px solid #7ec5bd;padding:2px 0 2px 12px}}.overview-card>small{{display:block;margin-top:10px}}.comparison-card header{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 0 12px;margin:0 0 10px;border-bottom:1px solid var(--line);box-shadow:none;border-radius:0}}.comparison-card h3{{margin:4px 0 0;font-size:20px}}.comparison-card header>b{{font-size:22px;color:var(--brand)}}.comparison-card.muted{{opacity:.32}}.comparison-facets{{grid-template-columns:repeat(3,minmax(0,1fr))}}.journey-stage-card>header{{display:flex;align-items:center;gap:14px;padding:0 0 12px;margin:0 0 12px;border-bottom:1px solid var(--line);box-shadow:none;border-radius:0}}.journey-stage-card h3{{margin:0;font-size:19px}}.journey-stage-card header p{{margin:2px 0;color:var(--muted)}}.stage-order{{font-size:20px;font-weight:800;color:#09828a;background:#e5f4ef;border-radius:12px;padding:8px 12px}}.stage-definition{{margin:6px 0}}.journey-cell-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:14px}}.journey-cell{{border-radius:12px;padding:12px;background:var(--soft)}}.journey-cell.not-observed{{background:#f5f5f0;color:#69777a}}.journey-cell h4{{margin:0 0 2px}}.cell-metric{{font-weight:700;color:var(--brand);margin:8px 0}}.headline-synthesis{{display:grid;grid-template-columns:1fr 1fr;gap:12px;background:var(--warm);padding:14px;border-radius:12px;margin-bottom:20px}}.headline-synthesis>div>b{{display:block;font-size:12px;color:#7b5a31;margin-bottom:7px}}.headline-synthesis .dual-zh{{font-size:16px}}.synthesis-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}.synthesis-card{{background:var(--soft);border-radius:12px;padding:13px;min-width:0}}.synthesis-card h5{{font-size:13px;margin:0 0 8px;color:#42636a}}.synthesis-card h5 small{{font-weight:500;margin-left:5px}}.synthesis-card p{{margin:0}}.trace-details{{border:1px dashed var(--line);border-radius:12px;margin:18px 0;padding:0 13px}}.trace-details>summary{{padding:12px 0;font-weight:650;color:#536a6d}}.trace-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding-bottom:12px}}.trace-row{{background:#fafbf9;padding:10px;border-radius:9px}}.trace-row b{{display:block;color:#66787b;font-size:12px}}.trace-row span{{display:block}}.three{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.jtbd{{font-size:16px;font-weight:650}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border:1px solid var(--line);vertical-align:top;text-align:left}}th{{background:#edf2f8}}.table-wrap{{overflow:auto}}.evidence-link{{color:var(--brand);background:white;margin:2px}}.notice{{border-left:4px solid var(--warn);padding-left:12px}}.report-nav{{display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:8px;z-index:3;background:#e9efecdd;backdrop-filter:blur(10px);padding:8px;margin:-8px 0 16px}}.report-nav a{{text-decoration:none;color:#255f65;background:#fff;border:1px solid var(--line);border-radius:99px;padding:7px 12px;font-size:12px}}.module-feature{{border-top:4px solid #77bdb2}}dialog{{max-width:650px;border:0;border-radius:12px;box-shadow:0 20px 60px #0005}}@media(max-width:900px){{.synthesis-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.comparison-facets{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}@media(max-width:700px){{main{{padding:18px 12px 56px}}header,.module{{padding:17px}}.stats,.overview-grid,.comparison-grid,.journey-stage-grid,.quad,.three,.synthesis-grid,.headline-synthesis{{grid-template-columns:1fr}}.comparison-facets,.journey-cell-grid,.trace-grid{{grid-template-columns:1fr}}summary{{display:block}}.metric{{text-align:left;margin-top:10px}}}}@media print{{body{{background:white}}.toolbar,.compare,.report-nav{{display:none}}.module,header,.overview-card,.comparison-card,.journey-stage-card{{break-inside:avoid;border-color:#bbb;box-shadow:none}}}}
</style><style>.v2-insight-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}}.v2-insight-card{{display:flex;gap:11px;align-items:flex-start;background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}}.v2-insight-card .dual-pair{{gap:7px}}.insight-index{{display:inline-flex;flex:0 0 30px;height:30px;align-items:center;justify-content:center;border-radius:10px;background:#d8eeeb;color:#087f8c;font-weight:750}}.journey-insight-pairs{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}.journey-insight-pairs>p{{background:var(--soft);border-radius:10px;padding:12px;margin:0}}.journey-insight-pairs b{{display:block;font-size:12px;color:#537079;margin-bottom:6px}}@media(max-width:700px){{.v2-insight-grid,.journey-insight-pairs{{grid-template-columns:1fr}}}}</style></head><body><main data-counts="{reviews},{events},{segment_count}"><header><h1>{title}</h1><p>{market} · {category}</p><p class="notice">本报告只基于本次上传的评论Excel；评论事实与统计可回查到来源评论。</p></header>
<nav class="report-nav" aria-label="报告章节"><a href="#module02">02 总览 / Overview</a><a href="#module03">03 细分详情 / Segments</a><a href="#module04">04 横向比较 / Compare</a><a href="#module05">05 用户旅程 / Journey</a><a href="#module06">06 探索信号 / Signals</a></nav>
<section class="module" id="module01"><h2>01 · 数据与方法概览</h2><div class="stats"><div><b>{reviews}</b>有效评论</div><div><b>{events}</b>语义事件</div><div><b>{segment_count}</b>全部细分</div><div><b>{tags}</b>规范概念</div><div><b>{raw_tags}</b>未归并原始表达</div></div><p>ASIN：{asins}；处理覆盖率：{coverage}；正式细分门槛：{threshold}条独立评论。</p><p>词项映射覆盖率：{normalized_coverage}；高频候选归并 {model_merged_values} 项，确定性的大小写/空白归一 {surface_normalized_values} 项。规范词典仍保留每个原词和事件回链。</p><p>未归并原始表达表示尚未确认语义归并，不等于独立需求概念；长尾和不确定词会保留原值，不强制合并。</p><p>证据范围：只使用本次上传的评论Excel；评论事实、引文与频率都来自评论行。画像与细分属于评论证据上的分析推理，不替代评论事实。</p><p>模型：{models}；Prompt：{prompts}</p><p><b>样本洞察 / Review-only overview</b>{overview_text}</p><p><b>方法摘要 / Method note</b>{method_note}</p>{gates}</section>
<section class="module module-feature" id="module02"><h2>02 · 全部细分总览</h2><p>先看被模型归纳后的双语人群、场景、动机与任务；出现率按独立评论统计。</p><div class="overview-grid">{overview}</div></section>
<section class="module module-feature" id="module03"><h2>03 · 全部细分详情</h2><div class="toolbar"><input id="search" placeholder="搜索中英文人群、场景、动机或任务"><button id="expand">展开全部</button><button id="collapse">收起全部</button><button onclick="print()">打印 / Print</button></div>{formal_cards}</section>
<section class="module module-feature" id="module04"><h2>04 · 细分横向比较</h2><p>各细分以同一套双语归纳维度并列，取消勾选可以暂时弱化某张卡片。</p><h3>细分差异与取舍 / Segment differences</h3>{segment_comparison}<h3>跨细分共性与冲突 / Cross-segment patterns and tensions</h3>{cross_cutting}<div class="comparison-grid">{compare}</div></section>
<section class="module module-feature" id="module05"><h2>05 · 动态用户旅程</h2><p>阶段由评论中的动作与进展模式归纳；缺席代表本次样本未观察到，不代表市场不存在。</p><div class="journey-stage-grid">{journey}</div></section>
<section class="module module-feature" id="module06"><h2>06 · 探索性信号</h2><p>低于正式门槛的评论线索，单独呈现并标记为探索性结论。名称、维度及需求摘要由模型抽象合并，并提供中英对照。</p><h3>待核查信号 / Signals to verify</h3>{exploratory_signals}{exploratory_cards}<h3>报告限制 / Report limitations</h3>{limitations}</section>
<dialog id="evidence"><h3>证据片段 / Evidence snippet</h3><p id="quote"></p><button onclick="this.closest('dialog').close()">关闭 / Close</button></dialog><script>
const cards=[...document.querySelectorAll('.segment-card')];document.querySelector('#search').addEventListener('input',e=>{{let q=e.target.value.trim().toLowerCase();cards.forEach(c=>c.hidden=q&&!c.dataset.search.toLowerCase().includes(q))}});document.querySelector('#expand').onclick=()=>cards.forEach(c=>c.open=true);document.querySelector('#collapse').onclick=()=>cards.forEach(c=>c.open=false);document.querySelectorAll('.compare').forEach(c=>c.onchange=()=>c.closest('.comparison-card').classList.toggle('muted',!c.checked));document.querySelectorAll('.evidence-link').forEach(b=>b.onclick=()=>{{document.querySelector('#quote').textContent=b.dataset.quote||'无片段';document.querySelector('#evidence').showModal()}});
</script></main></body></html>'''.format(
        title=esc("Amazon VOC 分析报告 %s" % (task.get("task_id") or "")), market=esc("%s / %s" % (task.get("marketplace", ""), task.get("country", ""))),
        category=esc(task.get("category") or "未指定品类"), reviews=counts.get("reviews", 0), events=counts.get("events", 0),
        segment_count=counts.get("segments", len(segments)), tags=counts.get("tags", 0),
        raw_tags=counts.get("raw_unmerged_tags", 0), asins=task.get("review_asin_count", 0),
        normalized_coverage="%.1f%%" % counts.get("normalized_value_coverage_pct", 0),
        model_merged_values=dictionary_summary.get("model_merged_values", 0),
        surface_normalized_values=dictionary_summary.get("surface_merged_values", 0),
        coverage="%.1f%%" % (100 if counts.get("reviews") else 0), threshold=method.get("minimum_formal_segment_reviews", 0),
        models=esc("、".join(method.get("models") or [])), prompts=esc(str(method.get("prompt_versions") or {})),
        gates=mini_table(["质量门禁", "结果", "异常数"], gate_rows),
        overview_text=bilingual((data.get("overview") or {}).get("zh"), (data.get("overview") or {}).get("en")),
        method_note=bilingual((data.get("method_note") or {}).get("zh"), (data.get("method_note") or {}).get("en")),
        overview=overview_cards or '<p class="empty">本次样本未形成细分 / No segments were identified.</p>',
        formal_cards="".join(segment_card(s, cells, stages) for s in formal) or '<p class="empty">没有达到正式门槛的细分。</p>',
        compare=comparison_cards or '<p class="empty">暂无可比较细分 / No segments to compare.</p>',
        segment_comparison=v2_insight_cards(data.get("segment_comparison")),
        cross_cutting=v2_insight_cards(data.get("cross_cutting")),
        journey=journey_cards,
        exploratory_signals=v2_insight_cards(data.get("exploratory_signals")),
        limitations=v2_insight_cards(data.get("limitations")),
        exploratory_cards="".join(segment_card(s, cells, stages) for s in exploratory) or '<p class="empty">没有探索性信号。</p>')
    common.ensure_dirs(workdir)
    output = common.output_path(workdir, "VOC_消费者需求分析_%s.html" % (task.get("task_id") or "TASK"))
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    return output


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--workdir", required=True); args = parser.parse_args()
    print("[build-html] %s" % build_html(args.workdir))


if __name__ == "__main__":
    main()
