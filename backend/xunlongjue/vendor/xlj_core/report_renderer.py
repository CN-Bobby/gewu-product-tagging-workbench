#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
寻龙诀 · 报告渲染器
====================
AI 不拼 HTML。只需提供数据 → 调 render() → 得到完整 HTML。
"""
import os, re, json
from html import escape
from datetime import datetime
from typing import List, Dict, Optional

from .site_profiles import format_currency, get_bestseller_url, normalize_site


DIRECTION_TO_LABEL = {
    "回归·双升": ("回归·双升", "badge-green"),
    "回归·销量升_价格微降": ("回归·量升", "badge-green"),
    "回归·销量微降_价格升": ("回归·价升", "badge-green"),
    "YoY·双升": ("YoY·双升", "badge-blue"),
    "YoY·销量升_价格微降": ("YoY·量升", "badge-blue"),
    "YoY·销量微降_价格升": ("YoY·价升", "badge-blue"),
    "双路径·双升": ("双路径·双升", "badge-blue"),
    "双路径·销量升_价格微降": ("双路径·量升", "badge-blue"),
    "双路径·销量微降_价格升": ("双路径·价升", "badge-blue"),
}


def _get_template_path() -> str:
    """获取模板文件路径"""
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(skill_dir, "templates", "report_template.html")


def _load_template() -> str:
    """加载 HTML 模板"""
    tpl_path = _get_template_path()
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


def _determine_label(sales_slope, price_slope, avg_sales, rule="", opp_level="", volume_threshold=10000) -> tuple:
    """判定标签 → (label_text, badge_class)
    
    7 种标签 (v2.4.3 增加路径前缀识别):
      回归·双升/量升/价升 (绿底) | YoY·双升/量升/价升 (蓝底)
      双路径·双升/量升/价升 (蓝底) | 潜力·高增 (琥珀底)
    """
    # P0: 低量高增长覆盖所有
    if avg_sales is not None and avg_sales < volume_threshold:
        return ("潜力·高增", "badge-potential")
    
    # v2.4.3: 优先从 rule 中提取已嵌入的路径前缀
    rule_str = str(rule)
    if rule_str in DIRECTION_TO_LABEL:
        return DIRECTION_TO_LABEL[rule_str]
    if rule_str.startswith("回归·"):
        prefix, badge = "回归", "badge-green"
    elif rule_str.startswith("YoY·"):
        prefix, badge = "YoY", "badge-blue"
    elif rule_str.startswith("双路径·"):
        prefix, badge = "双路径", "badge-blue"
    else:
        # 旧版方向格式兼容: 不含前缀时根据内容推断
        is_reg = "回归" in rule_str or not ("YoY" in rule_str or "yoy" in rule_str.lower())
        prefix = "回归" if is_reg else "YoY"
        badge = "badge-green" if is_reg else "badge-blue"
    
    if "双升" in rule_str:
        return (f"{prefix}·双升", badge)
    if "量升" in rule_str or ("销量升" in rule_str and "价格微降" in rule_str):
        return (f"{prefix}·量升", badge)
    if "价升" in rule_str or ("均价升" in rule_str and "销量微降" in rule_str):
        return (f"{prefix}·价升", badge)
    
    # 兜底: 从数值推测
    if sales_slope is not None and price_slope is not None and sales_slope > 0 and price_slope > 0:
        return ("回归·双升", "badge-green")
    if sales_slope is not None and sales_slope > 0:
        return ("回归·量升", "badge-green")
    if price_slope is not None and price_slope > 0:
        return ("回归·价升", "badge-green")
    if rule_str:
        return (rule_str, "badge-warning")
    return ("末端机会", "badge-green")


def _determine_detail_label(result: str, avg_sales, volume_threshold: float = 10000) -> tuple:
    """将 v4 节点终态映射成稳定、可读的报告标签。"""
    if "低体量" in result or "POLICY_PRUNED" in result:
        return ("低体量观察", "badge-potential")
    if "短历史" in result or "SHORT_HISTORY" in result:
        return ("短历史", "badge-warning")
    if "深度上限" in result or "DEPTH_LIMIT" in result:
        return ("深度上限", "badge-warning")
    if "暂停" in result or "SUSPENDED" in result:
        return ("暂停", "badge-eliminated")
    if "已展开" in result or "EXPANDED" in result:
        return ("已展开", "badge-drill")
    if "淘汰" in result:
        return ("淘汰", "badge-eliminated")
    if "下钻" in result or "待获取" in result or "待处理" in result:
        return ("待下钻", "badge-drill")
    if "末端" in result or "机会" in result or "OPPORTUNITY" in result:
        if avg_sales is not None and avg_sales < volume_threshold:
            return ("潜力", "badge-potential")
        return ("末端机会", "badge-opp")
    if "数据不足" in result or "NEED_DATA" in result or "数据" in result:
        return ("数据不足", "badge-drill")
    return (result[:6] if len(result) > 6 else result, "badge-other")


def _fmt(val, suffix="%", precision=2) -> str:
    """格式化数值"""
    if val is None:
        return ""
    return f"{val:+.{precision}f}{suffix}"


def _fmt_num(val, precision=0, site=None) -> str:
    """格式化整数"""
    if val is None:
        return ""
    if precision == 0 or not site:
        return f"{val:,.0f}"
    return format_currency(val, site, precision)


def _build_opp_row(o: dict, site: str, volume_threshold: float = 10000) -> str:
    """构建机会节点表格行"""
    label, lbl_cls = _determine_label(
        o.get("sales_slope"), o.get("price_slope"),
        o.get("avg_sales"), o.get("rule", ""), o.get("opp_level", ""), volume_threshold)
    nm = escape(str(o["path"]).split("/")[-1].strip())
    node_id = escape(str(o["id"]), quote=True)
    lk = escape(get_bestseller_url(site, o["id"]), quote=True)
    lv = o["level"]
    lvc = f'badge-l{2 if lv<=2 else 3 if lv<=3 else 4}'
    return f'<tr data-id="{node_id}"><td><button type="button" class="star-btn" data-nid="{node_id}" aria-label="收藏节点 {node_id}" aria-pressed="false">☆</button></td><td><a class="node-link" href="{lk}" target="_blank" rel="noopener noreferrer">{node_id}</a></td><td>{nm}<span class="note-icon" data-nid="{node_id}">📝</span></td><td><span class="{lvc}">L{lv}</span></td><td><span class="badge {lbl_cls}">{escape(label)}</span></td><td class="num">{_fmt(o.get("sales_slope"))}</td><td class="num">{_fmt(o.get("price_slope"))}</td><td class="num">{_fmt(o.get("yoy_sales"))}</td><td class="num">{_fmt(o.get("yoy_price"))}</td><td class="num">{_fmt_num(o.get("avg_sales"), 0)}</td><td class="num">{_fmt_num(o.get("avg_price"), 2, site)}</td></tr>'


def _build_proc_row(p: dict, site: str, volume_threshold: float = 10000) -> str:
    """构建已处理节点表格行"""
    label, lbl_cls = _determine_detail_label(p.get("result", ""), p.get("avg_sales"), volume_threshold)
    node_id = escape(str(p["id"]), quote=True)
    lk = escape(get_bestseller_url(site, p["id"]), quote=True)
    path = escape(str(p["path"]))
    return f'<tr data-id="{node_id}"><td><button type="button" class="star-btn" data-nid="{node_id}" aria-label="收藏节点 {node_id}" aria-pressed="false">☆</button></td><td><a class="node-link" href="{lk}" target="_blank" rel="noopener noreferrer">{node_id}</a></td><td>{path}<span class="note-icon" data-nid="{node_id}">📝</span></td><td><span class="badge {lbl_cls}">{escape(label)}</span></td><td class="num">{_fmt(p.get("sales_slope"))}</td><td class="num">{_fmt(p.get("price_slope"))}</td><td class="num">{_fmt(p.get("yoy_sales"))}</td><td class="num">{_fmt(p.get("yoy_price"))}</td><td class="num">{_fmt_num(p.get("avg_sales"), 0)}</td><td class="num">{_fmt_num(p.get("avg_price"), 2, site)}</td></tr>'


def load_report_data(data_dir: str, site: str):
    """从标准 Excel 表头读取报告数据，避免 AI 手工映射列。"""
    from openpyxl import load_workbook

    site = normalize_site(site)

    def rows(filename):
        path = os.path.join(data_dir, filename)
        if not os.path.exists(path):
            return []
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        headers = [str(cell.value or "").strip() for cell in ws[1]]
        records = []
        for values in ws.iter_rows(min_row=2, values_only=True):
            item = dict(zip(headers, values))
            if not item.get("类目ID") or not item.get("站点"):
                continue
            if normalize_site(item.get("站点")) != site:
                continue
            records.append(item)
        wb.close()
        return records

    procs = [
        {
            "id": str(item["类目ID"]),
            "path": item.get("完整路径") or "",
            "level": int(item.get("层级") or 0),
            "result": item.get("处理结果") or "",
            "sales_slope": item.get("销量年化斜率(%)"),
            "price_slope": item.get("均价年化斜率(%)"),
            "yoy_sales": item.get("YoY销量增长率(%)"),
            "yoy_price": item.get("YoY均价增长率(%)"),
            "avg_sales": item.get("平均月销量"),
            "avg_price": item.get("平均月均价"),
        }
        for item in rows("03_已处理节点表.xlsx")
    ]
    opps = [
        {
            "id": str(item["类目ID"]),
            "path": item.get("完整路径") or "",
            "level": int(item.get("层级") or 0),
            "sales_slope": item.get("销量年化斜率(%)"),
            "price_slope": item.get("均价年化斜率(%)"),
            "yoy_sales": item.get("YoY销量增长率(%)"),
            "yoy_price": item.get("YoY均价增长率(%)"),
            "avg_sales": item.get("平均月销量"),
            "avg_price": item.get("平均月均价"),
            "rule": item.get("通过规则") or "",
            "opp_level": item.get("机会级别") or "",
        }
        for item in rows("04_机会类目结果表.xlsx")
    ]
    return opps, procs


def render_report(
    opps: List[dict],
    procs: List[dict],
    candidates: Optional[List[dict]] = None,
    m2_html: str = "",
    audit_html: str = "",
    site: str = None,
    category: str = "",
    init_node: str = "",
    mcp_count: int = 0,
    report_mode: str = "final",
    data_dir: str = None,
    storage_key: str = "",
    volume_threshold: float = 10000,
) -> str:
    """
    渲染完整报告 HTML。
    
    参数:
        opps: 机会节点列表，回归指标用 sales_slope/price_slope，
              YoY 指标用 yoy_sales/yoy_price（均为百分比）。
        procs: 已处理节点列表，字段同上，另含 result。
        m2_html: M2 品类洞察 HTML (AI 按 m2-insight-standard.md 生成)
        site: 站点代码
        category: 品类名
        init_node: 初始节点ID
        mcp_count: MCP 调用次数
        report_mode: final 或 partial；SQLite完成闸门由调用者执行
        data_dir: v4兼容参数，不参与状态判断
    
    返回: 完整 HTML 字符串
    """
    site = normalize_site(site)
    report_mode = str(report_mode or "").strip().lower()
    if report_mode not in {"final", "partial"}:
        raise ValueError("report_mode 只能是 final 或 partial")
    now = datetime.now()
    date_str = now.strftime("%y%m%d")
    date_full = now.strftime("%Y-%m-%d %H:%M")
    
    candidates = candidates or []
    total = len(procs)
    total_opps = len(opps)
    eliminated = sum(1 for p in procs if "淘汰" in str(p.get("result", "")))
    
    from collections import Counter
    lv_cnt = Counter(p.get("level", 0) for p in procs)
    l4plus_count = sum(count for level, count in lv_cnt.items() if level >= 4)
    
    hi = [o for o in opps if o.get("avg_sales") is not None and o["avg_sales"] >= volume_threshold]
    lo = [o for o in opps if o.get("avg_sales") is not None and o["avg_sales"] < volume_threshold]
    nd = [o for o in opps if o.get("avg_sales") is None]
    
    # 构建节点视图 HTML
    high_rows = "\n".join(_build_opp_row(o, site, volume_threshold) for o in hi)
    low_rows = "\n".join(_build_opp_row(o, site, volume_threshold) for o in lo)
    nd_rows = "\n".join(_build_opp_row(o, site, volume_threshold) for o in nd) if nd else ""
    threshold_text = f"{volume_threshold:,.0f}"
    
    l1_rows = "\n".join(_build_proc_row(p, site, volume_threshold) for p in procs if p.get("level") == 1)
    l2_rows = "\n".join(_build_proc_row(p, site, volume_threshold) for p in procs if p.get("level") == 2)
    l3_rows = "\n".join(_build_proc_row(p, site, volume_threshold) for p in procs if p.get("level") == 3)
    l4_rows = "\n".join(_build_proc_row(p, site, volume_threshold) for p in procs if p.get("level", 0) >= 4)
    
    hi_section = ""
    if hi:
        hi_section = f'''<div class='section'><h2>机会节点 — 月均销量 ≥ {threshold_text}</h2><div class='table-wrap'><table id='highOppTable'><thead><tr><th>⭐</th><th onclick="sortTable('highOppTable',1)">节点ID</th><th>名称</th><th>Lv</th><th>判定标签</th><th onclick="sortTable('highOppTable',5,'num')">回归销量斜率</th><th onclick="sortTable('highOppTable',6,'num')">回归均价斜率</th><th onclick="sortTable('highOppTable',7,'num')">YoY销量</th><th onclick="sortTable('highOppTable',8,'num')">YoY均价</th><th onclick="sortTable('highOppTable',9,'num')">月均销量</th><th onclick="sortTable('highOppTable',10,'num')">月均价</th></tr></thead><tbody id='highOppBody' class='filter-body'>{high_rows}</tbody></table></div></div>'''
    
    lo_section = ""
    if lo:
        lo_section = f'''<div class='section'><h2>机会节点 — 月均销量 &lt; {threshold_text}</h2><div class='table-wrap'><table id='lowOppTable'><thead><tr><th>⭐</th><th onclick="sortTable('lowOppTable',1)">节点ID</th><th>名称</th><th>Lv</th><th>判定标签</th><th onclick="sortTable('lowOppTable',5,'num')">回归销量斜率</th><th onclick="sortTable('lowOppTable',6,'num')">回归均价斜率</th><th onclick="sortTable('lowOppTable',7,'num')">YoY销量</th><th onclick="sortTable('lowOppTable',8,'num')">YoY均价</th><th onclick="sortTable('lowOppTable',9,'num')">月均销量</th><th onclick="sortTable('lowOppTable',10,'num')">月均价</th></tr></thead><tbody id='lowOppBody' class='filter-body'>{low_rows}</tbody></table></div></div>'''
    
    nd_section = ""
    if nd:
        nd_section = f'''<div class='section'><h2>数据不足 ({len(nd)} 个)</h2><div class='table-wrap'><table><thead><tr><th>⭐</th><th>节点ID</th><th>名称</th><th>Lv</th><th>判定标签</th><th>回归销量斜率</th><th>回归均价斜率</th><th>YoY销量</th><th>YoY均价</th><th>月均销量</th><th>月均价</th></tr></thead><tbody class='filter-body'>{nd_rows}</tbody></table></div></div>'''

    candidate_section = ""
    if candidates:
        candidate_rows = "\n".join(_build_opp_row(o, site, volume_threshold) for o in candidates)
        candidate_section = f'''<div class='section'><h2>低体量增长候选 — 非正式机会</h2><div class='candidate-note'>近6月均销低于策略门槛 {threshold_text}，因此停止下钻；这里仅保留增长观察，不计入正式机会。</div><div class='table-wrap'><table id='candidateTable'><thead><tr><th>⭐</th><th onclick="sortTable('candidateTable',1)">节点ID</th><th>名称</th><th>Lv</th><th>判定标签</th><th onclick="sortTable('candidateTable',5,'num')">回归销量斜率</th><th onclick="sortTable('candidateTable',6,'num')">回归均价斜率</th><th onclick="sortTable('candidateTable',7,'num')">YoY销量</th><th onclick="sortTable('candidateTable',8,'num')">YoY均价</th><th onclick="sortTable('candidateTable',9,'num')">近6月均销</th><th onclick="sortTable('candidateTable',10,'num')">月均价</th></tr></thead><tbody id='candidateBody' class='filter-body'>{candidate_rows}</tbody></table></div></div>'''
    
    mode_banner = (
        '<div class="partial-banner">阶段报告：当前运行尚未完成，数据可能继续变化。</div>'
        if report_mode == "partial" else ""
    )
    node_view = f'''{mode_banner}{audit_html}<div class="stats">
<div class="stat-card stat-blue"><div class="num">{total}</div><div class="label">已处理</div><div class="sublabel">L1:{lv_cnt.get(1,0)} L2:{lv_cnt.get(2,0)} L3:{lv_cnt.get(3,0)} L4+:{l4plus_count}</div></div>
<div class="stat-card stat-green"><div class="num">{total_opps}</div><div class="label">正式机会</div><div class="sublabel">高体量:{len(hi)} 其他:{len(lo)+len(nd)}</div></div>
<div class="stat-card stat-red"><div class="num">{eliminated}</div><div class="label">淘汰</div></div>
<div class="stat-card stat-purple"><div class="num">{mcp_count}</div><div class="label">MCP 调用</div></div>
</div>
<div class="toolbar">
<button class="btn-action" onclick="exportData()">📤 导出</button>
<button class="btn-action" onclick="document.getElementById('imp').click()">📥 导入</button>
<input type="file" id="imp" accept=".json" style="display:none" onchange="importData(event)">
<button class="btn-action" onclick="clearData()">🗑 清空</button>
<button class="btn-action" id="fsb" onclick="filterStar()">⭐ 仅星标</button>
<button class="btn-action" id="fnb" onclick="filterNote()">📝 有笔记</button>
<button class="btn-action" onclick="showAll()">全部</button>
<input type="text" class="search-box" placeholder="🔍 搜索节点...">
<span class="count-display" id="cd">⭐0个·📝0条</span>
</div>
<div class="group-counter">正式机会: <strong>{total_opps}</strong> | 低体量增长候选: <strong>{len(candidates)}</strong>{" | 无数据机会: <strong>" + str(len(nd)) + "</strong>" if nd else ""}</div>
{hi_section}{lo_section}{nd_section}{candidate_section}
<div class='section'><h2>全量节点明细 ({total} 个)</h2>
<div class='collapse'><div class='collapse-header' onclick="this.parentElement.classList.toggle('open')"><span class='arrow'>▶</span> L1 ({lv_cnt.get(1,0)} 个)</div><div class='collapse-body'><div class='table-wrap'><table><thead><tr><th>⭐</th><th>节点ID</th><th>路径</th><th>标签</th><th>回归销量斜率</th><th>回归均价斜率</th><th>YoY销量</th><th>YoY均价</th><th>近6月均销</th><th>月均价</th></tr></thead><tbody class='filter-body'>{l1_rows}</tbody></table></div></div></div>
<div class='collapse'><div class='collapse-header' onclick="this.parentElement.classList.toggle('open')"><span class='arrow'>▶</span> L2 ({lv_cnt.get(2,0)} 个)</div><div class='collapse-body'><div class='table-wrap'><table><thead><tr><th>⭐</th><th>节点ID</th><th>路径</th><th>标签</th><th>回归销量斜率</th><th>回归均价斜率</th><th>YoY销量</th><th>YoY均价</th><th>月均销量</th><th>月均价</th></tr></thead><tbody class='filter-body'>{l2_rows}</tbody></table></div></div></div>
<div class='collapse'><div class='collapse-header' onclick="this.parentElement.classList.toggle('open')"><span class='arrow'>▶</span> L3 ({lv_cnt.get(3,0)} 个)</div><div class='collapse-body'><div class='table-wrap'><table><thead><tr><th>⭐</th><th>节点ID</th><th>路径</th><th>标签</th><th>回归销量斜率</th><th>回归均价斜率</th><th>YoY销量</th><th>YoY均价</th><th>月均销量</th><th>月均价</th></tr></thead><tbody class='filter-body'>{l3_rows}</tbody></table></div></div></div>
<div class='collapse'><div class='collapse-header' onclick="this.parentElement.classList.toggle('open')"><span class='arrow'>▶</span> L4+ ({l4plus_count} 个)</div><div class='collapse-body'><div class='table-wrap'><table><thead><tr><th>⭐</th><th>节点ID</th><th>路径</th><th>标签</th><th>回归销量斜率</th><th>回归均价斜率</th><th>YoY销量</th><th>YoY均价</th><th>月均销量</th><th>月均价</th></tr></thead><tbody class='filter-body'>{l4_rows}</tbody></table></div></div></div>
</div>
<div class='footer'><table>
<tr><td>数据来源</td><td>Sorftime MCP</td></tr>
<tr><td>判定标签</td><td>回归路径(绿底) | YoY/双路径(蓝底) | 潜力·高增(琥珀底) | 淘汰(红底)</td></tr>
<tr><td>趋势指标</td><td>回归年化斜率与 YoY 年增长率分列展示，均为百分比</td></tr>
<tr><td>交互功能</td><td>星标收藏 · 笔记注解 · 排序 · 搜索筛选 · JSON导入/导出 · localStorage</td></tr>
<tr><td>体量口径</td><td>近6个完整连续月平均销量；当前策略门槛 {threshold_text}，低于门槛停止下钻，候选不计入正式机会</td></tr>
<tr><td>生成时间</td><td>{date_full}</td></tr>
</table></div>'''
    
    # 替换模板占位符
    tpl = _load_template()
    html = tpl.replace("{{CATEGORY}}", escape(category.replace("_", " ") if category else "Unknown"))
    html = html.replace("{{SITE}}", escape(site))
    html = html.replace("{{DATE}}", date_str)
    html = html.replace("{{INIT_NODE}}", escape(str(init_node)))
    html = html.replace("{{NODE_VIEW}}", node_view)
    html = html.replace("{{M2_VIEW}}", m2_html or "<div class='m2-section'><p style='padding:40px;text-align:center;color:#86868b'>品类结构洞察将在后续版本中提供</p></div>")
    html = html.replace("{{STORAGE_KEY_JSON}}", json.dumps(storage_key or f"xlj_{init_node}", ensure_ascii=False))
    html = html.replace("{{EXPORT_NAME_JSON}}", json.dumps(f"xlj_{category}_{date_str}.json", ensure_ascii=False))
    
    return html


def render_workspace_report(data_dir: str, site: str, category: str,
                            init_node: str, m2_html: str = "",
                            report_mode: str = "final") -> str:
    """从工作区确定性读取数据、统计 MCP 调用并渲染最终或阶段报告。"""
    from harness.writer import Writer

    opps, procs = load_report_data(data_dir, site)
    writer = Writer(data_dir)
    try:
        mcp_count = writer.get_stats(site)["mcp_calls"]
    finally:
        writer.close()
    return render_report(
        opps,
        procs,
        m2_html=m2_html,
        site=site,
        category=category,
        init_node=init_node,
        mcp_count=mcp_count,
        report_mode=report_mode,
        data_dir=data_dir,
    )


def save_report(html: str, output_dir: str, site: str, category: str) -> str:
    """保存 HTML 到指定输出目录，返回文件路径。"""
    now = datetime.now()
    date_str = now.strftime("%y%m%d")
    site = normalize_site(site)
    safe_category = re.sub(r'[<>:"/\\|?*]+', "_", category).strip(" .") or "category"
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{site}_{date_str}_{safe_category}.html"
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[Renderer] 报告已保存: {path}")
    return path
