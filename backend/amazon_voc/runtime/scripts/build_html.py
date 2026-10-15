# -*- coding: utf-8 -*-
"""Render the V4 six-module frozen HTML from cache/analysis.json."""
import argparse
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from compile_analysis import compile_analysis


def esc(value):
    return html.escape(str(value or ""), quote=True)


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


def segment_card(segment, cells, stages):
    key, metrics = segment.get("segment_key") or {}, segment.get("metrics") or {}
    status = "正式" if segment.get("status") == "formal" else "探索"
    audience = segment.get("audience_details") or {}
    stage_by_id = {row.get("stage_id"): row for row in cells if row.get("segment_id") == segment.get("segment_id")}
    journey_rows = []
    for stage in stages:
        row = stage_by_id.get(stage.get("stage_id"), {})
        journey_rows.append([esc(stage.get("name")), esc(row.get("status", "not_observed")),
                             esc(row.get("review_count", 0)), labels(row.get("actions")), labels(row.get("goals")),
                             labels(row.get("pains")), labels(row.get("itches"))])
    workaround_rows = [[esc(w.get("standardized_action") or w.get("raw_action")), esc(w.get("strategy")),
                        esc("、".join(w.get("costs") or [])), esc(w.get("effect"))]
                       for w in (segment.get("workarounds") or [])[:12]]
    priority_rows = [[esc(row.get("need")), esc(row.get("priority_type")), esc(row.get("basis")), esc(row.get("confidence"))]
                     for row in segment.get("need_priorities") or []]
    return '''<details class="segment-card" data-segment-id="{sid}" data-status="{raw_status}" data-search="{search}">
<summary><div><span class="pill">{status}</span><h3>{name}</h3><div class="dims">{audience_key} · {scenario} · {motivation} · {task}</div></div>
<div class="metric"><b>{rate:.1f}%</b><span>{reviews}条评论 / {events}事件 / {asins} ASIN</span></div></summary>
<div class="card-body"><div class="quad"><div><b>人群</b>{audience_key}</div><div><b>场景</b>{scenario}</div><div><b>动机</b>{motivation}</div><div><b>任务</b>{task}</div></div>
<h4>核心 JTBD</h4><p class="jtbd">{jtbd}</p><p>{persona}</p>
<h4>Buyer / User 与角色限制</h4><p><b>实际使用者：</b>{users}<br><b>购买者：</b>{buyers}<br><b>关系：</b>{relations}<br><b>物理限制：</b>{constraints}<br><b>熟练度：</b>{skills}<br><b>角色诉求：</b>{roles}</p>
<div class="three"><section><h4>痛点</h4><p>{pains}</p></section><section><h4>痒点</h4><p>{itches}</p></section><section><h4>期望</h4><p>{outcomes}</p></section></div>
<h4>类 KANO 推断</h4><p class="boundary">仅按评论证据组织优先级，不是正式 KANO 问卷结论。</p>{priorities}
<h4>Workaround：动作、策略、成本与效果</h4>{workarounds}
<h4>本细分旅程差异</h4>{journey}
<h4>证据回查</h4><p>{evidence}</p></div></details>'''.format(
        sid=esc(segment.get("segment_id")), raw_status=esc(segment.get("status")), status=status,
        search=esc(" ".join(str(v) for v in [segment.get("name"), *key.values()])), name=esc(segment.get("name")),
        audience_key=esc(key.get("audience")), scenario=esc(key.get("scenario")), motivation=esc(key.get("motivation")),
        task=esc(key.get("task")), rate=float(metrics.get("review_coverage", 0))*100,
        reviews=metrics.get("review_count", 0), events=metrics.get("event_count", 0), asins=metrics.get("asin_count", 0),
        jtbd=esc(segment.get("jtbd_statement")), persona=esc(segment.get("persona_summary")),
        users=labels(audience.get("users")), buyers=labels(audience.get("buyers")), relations=labels(audience.get("relationships")),
        constraints=labels(audience.get("physical_constraints")), skills=labels(audience.get("skill_levels")), roles=labels(audience.get("role_needs")),
        pains=labels(segment.get("pains")), itches=labels(segment.get("itches")), outcomes=labels(segment.get("desired_outcomes")),
        priorities=mini_table(["需求", "类型", "依据", "置信度"], priority_rows),
        workarounds=mini_table(["动作", "策略", "成本", "效果"], workaround_rows),
        journey=mini_table(["阶段", "状态", "评论", "动作", "目标", "痛点", "痒点"], journey_rows),
        evidence=evidence_links(segment.get("evidence_snippets")))


def build_html(workdir):
    path = common.cache_path(workdir, "analysis.json")
    data = common.read_json(path) if os.path.exists(path) else compile_analysis(workdir)
    task, method = data.get("task") or {}, data.get("method") or {}
    segments = data.get("segments") or []
    formal = [s for s in segments if s.get("status") == "formal"]
    exploratory = [s for s in segments if s.get("status") != "formal"]
    journey = data.get("journey") or {}; stages, cells = journey.get("stages") or [], journey.get("cells") or []
    counts = data.get("counts") or {"reviews": task.get("review_count", 0), "events": 0, "segments": len(segments)}
    quality = data.get("quality") or {}
    gate_rows = [[esc(g.get("name")), "通过" if g.get("pass") else "失败", esc(g.get("failed_count", 0))]
                 for g in quality.get("gates") or []]
    overview_rows, compare_rows = [], []
    for s in segments:
        key, metrics = s.get("segment_key") or {}, s.get("metrics") or {}
        overview_rows.append([esc(s.get("name")), esc("正式" if s.get("status") == "formal" else "探索"),
                              esc(key.get("audience")), esc(key.get("scenario")), esc(key.get("motivation")), esc(key.get("task")),
                              esc(metrics.get("review_count", 0)), esc("%.1f%%" % (float(metrics.get("review_coverage", 0))*100)),
                              esc(metrics.get("event_count", 0)), esc(metrics.get("asin_count", 0)), esc(s.get("jtbd_statement"))])
        compare_rows.append('<tr data-compare-row="%s"><td><input class="compare" type="checkbox" checked></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                            (esc(s.get("segment_id")), esc(s.get("name")), esc(key.get("audience")), esc(key.get("task")),
                             esc(key.get("motivation")), labels(s.get("pains")[:3]), labels(s.get("itches")[:3]),
                             labels((s.get("desired_outcomes") or [])[:3]),
                             labels([w.get("standardized_action") or w.get("raw_action") for w in (s.get("workarounds") or [])[:3]])))
    stage_head = "".join("<th>%s</th>" % esc(s.get("name")) for s in stages)
    matrix, lookup = [], {(c.get("segment_id"), c.get("stage_id")): c for c in cells}
    for segment in segments:
        values = []
        for stage in stages:
            cell = lookup.get((segment.get("segment_id"), stage.get("stage_id")), {})
            values.append("<td><b>%s</b><small>%s</small><div>%s</div></td>" %
                          (esc(cell.get("review_count", 0)), "有证据" if cell.get("status") == "observed" else "缺席",
                           labels(cell.get("actions"), "")))
        matrix.append("<tr><th>%s</th>%s</tr>" % (esc(segment.get("name")), "".join(values)))
    html_text = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>
:root{{--ink:#162033;--muted:#667085;--line:#d9e1eb;--brand:#185adb;--soft:#f4f7fb;--warn:#a64b00}}*{{box-sizing:border-box}}body{{margin:0;background:#eef2f7;color:var(--ink);font:14px/1.65 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1240px;margin:auto;padding:28px 20px 70px}}header,.module{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:20px}}h1,h2,h3,h4{{line-height:1.3}}h1,h2{{margin-top:0}}.stats,.quad,.three{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.stats div,.quad div,.three section{{background:var(--soft);border-radius:10px;padding:12px}}.stats b,.quad b{{display:block}}.stats b{{font-size:22px}}.toolbar{{display:flex;gap:8px;margin:12px 0}}input,button{{padding:8px 10px;border:1px solid var(--line);border-radius:8px}}button{{cursor:pointer}}.segment-card{{border:1px solid var(--line);border-radius:12px;margin:10px 0;background:white}}summary{{display:flex;justify-content:space-between;gap:18px;padding:16px;cursor:pointer}}summary h3{{display:inline;margin:0 8px}}.card-body{{border-top:1px solid var(--line);padding:18px}}.pill{{background:#e8f0ff;color:#174ea6;padding:3px 8px;border-radius:99px}}.metric{{text-align:right}}.metric b,.metric span{{display:block}}.metric b{{font-size:23px}}.dims,.empty,.boundary,small{{color:var(--muted)}}.three{{grid-template-columns:repeat(3,1fr)}}.jtbd{{font-size:16px;font-weight:650}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border:1px solid var(--line);vertical-align:top;text-align:left}}th{{background:#edf2f8}}.table-wrap{{overflow:auto}}.evidence-link{{color:var(--brand);background:white;margin:2px}}.notice{{border-left:4px solid var(--warn);padding-left:12px}}dialog{{max-width:650px;border:0;border-radius:12px;box-shadow:0 20px 60px #0005}}@media(max-width:760px){{.stats,.quad,.three{{grid-template-columns:1fr}}summary{{display:block}}}}@media print{{body{{background:white}}.toolbar,.compare{{display:none}}.module,header{{break-inside:avoid;border-color:#bbb}}}}
</style></head><body><main data-counts="{reviews},{events},{segment_count}"><header><h1>{title}</h1><p>{market} · {category}</p><p class="notice">本报告只基于本次上传的评论Excel；评论事实与统计可回查到来源评论。</p></header>
<section class="module"><h2>01 · 数据与方法概览</h2><div class="stats"><div><b>{reviews}</b>有效评论</div><div><b>{events}</b>语义事件</div><div><b>{segment_count}</b>全部细分</div><div><b>{tags}</b>模型归一标签</div><div><b>{raw_tags}</b>未归并原始表达</div></div><p>ASIN：{asins}；处理覆盖率：{coverage}；正式门槛：{threshold}条独立评论。</p><p>证据范围：只使用本次上传的评论Excel；评论事实、引文与频率都来自评论行。类 KANO、画像与细分属于评论证据上的分析推理，不替代评论事实。</p><p>未归并原始表达仅为字段原值的直接映射，不代表模型已完成同义词归并或术语标准化。</p><p>模型：{models}；Prompt：{prompts}</p>{gates}</section>
<section class="module"><h2>02 · 全部细分总览</h2>{overview}</section>
<section class="module"><h2>03 · 全部细分详情</h2><div class="toolbar"><input id="search" placeholder="搜索人群、场景、动机或任务"><button id="expand">展开全部</button><button id="collapse">收起全部</button><button onclick="print()">打印</button></div>{formal_cards}</section>
<section class="module"><h2>04 · 细分横向比较</h2><p>取消勾选可暂时弱化不需要比较的细分。</p><div class="table-wrap"><table><thead><tr><th>显示</th><th>细分</th><th>人群</th><th>任务</th><th>动机</th><th>痛点</th><th>痒点</th><th>期望</th><th>Workaround</th></tr></thead><tbody>{compare}</tbody></table></div></section>
<section class="module"><h2>05 · 动态用户旅程</h2><p>阶段由当前品类动作生成；缺席代表本次样本未观察到，不代表市场不存在。</p><div class="table-wrap"><table><thead><tr><th>细分</th>{stage_head}</tr></thead><tbody>{matrix}</tbody></table></div></section>
<section class="module"><h2>06 · 探索性信号</h2><p>低于正式门槛的评论线索，单独呈现，避免将小样本信号当作稳定结论。</p>{exploratory_cards}</section>
<dialog id="evidence"><h3>证据片段</h3><p id="quote"></p><button onclick="this.closest('dialog').close()">关闭</button></dialog><script>
const cards=[...document.querySelectorAll('.segment-card')];document.querySelector('#search').addEventListener('input',e=>{{let q=e.target.value.trim().toLowerCase();cards.forEach(c=>c.hidden=q&&!c.dataset.search.toLowerCase().includes(q))}});document.querySelector('#expand').onclick=()=>cards.forEach(c=>c.open=true);document.querySelector('#collapse').onclick=()=>cards.forEach(c=>c.open=false);document.querySelectorAll('.compare').forEach(c=>c.onchange=()=>c.closest('tr').style.opacity=c.checked?'1':'.25');document.querySelectorAll('.evidence-link').forEach(b=>b.onclick=()=>{{document.querySelector('#quote').textContent=b.dataset.quote||'无片段';document.querySelector('#evidence').showModal()}});
</script></main></body></html>'''.format(
        title=esc("Amazon VOC 分析报告 %s" % (task.get("task_id") or "")), market=esc("%s / %s" % (task.get("marketplace", ""), task.get("country", ""))),
        category=esc(task.get("category") or "未指定品类"), reviews=counts.get("reviews", 0), events=counts.get("events", 0),
        segment_count=counts.get("segments", len(segments)), tags=counts.get("tags", 0),
        raw_tags=counts.get("raw_unmerged_tags", 0), asins=task.get("review_asin_count", 0),
        coverage="%.1f%%" % (100 if counts.get("reviews") else 0), threshold=method.get("minimum_formal_segment_reviews", 0),
        models=esc("、".join(method.get("models") or [])), prompts=esc(str(method.get("prompt_versions") or {})),
        gates=mini_table(["质量门禁", "结果", "异常数"], gate_rows),
        overview=mini_table(["细分", "状态", "人群", "场景", "动机", "任务", "评论", "出现率", "事件", "ASIN", "核心JTBD"], overview_rows),
        formal_cards="".join(segment_card(s, cells, stages) for s in formal) or '<p class="empty">没有达到正式门槛的细分。</p>',
        compare="".join(compare_rows), stage_head=stage_head, matrix="".join(matrix),
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
