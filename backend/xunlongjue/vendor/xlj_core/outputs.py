"""SQLite-backed HTML and Excel outputs."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from html import escape
from typing import Any, List

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from . import __version__
from .db import SCHEMA_VERSION, connect, require_run, utc_now
from .engine import list_runs, status
from .insights import scan_insights
from .report_renderer import render_report
from .site_profiles import get_bestseller_url

REPORT_CONTRACT = "xlj-mature-v3.3.1"
REPORT_REQUIRED_MARKERS = (
    f'data-report-contract="{REPORT_CONTRACT}"', 'data-renderer-version="4"',
    "function sortTable", "function openNote", "function filterStar",
    "function filterNote", "function applyFilters", "function exportData",
    "function importData", "扫描过程洞察", "回归销量斜率", "YoY销量",
    "本次运行说明", "localStorage",
)


def validate_report_contract(document: str) -> None:
    missing = [marker for marker in REPORT_REQUIRED_MARKERS if marker not in document]
    if missing:
        raise RuntimeError("报告功能契约校验失败，禁止发布降级报告；缺少: " + ", ".join(missing))


def _safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", str(value)).strip(" .") or "category"


def _bs_link(site: str, node_id: str) -> str:
    return get_bestseller_url(site, node_id)


def _node_rows(conn, run_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT n.node_id,n.path,n.level,n.state,n.result,n.error_reason,n.history_months,n.current_scale,"
        "CASE WHEN n.node_id=r.root_node_id THEN 1 ELSE 0 END AS is_root,"
        "d.direction,d.sales_slope,d.price_slope,d.yoy_sales,d.yoy_price,d.avg_sales,d.avg_price,"
        "d.sales_r2,d.price_r2,d.projected_scale,d.opportunity_level,d.reason "
        "FROM nodes n JOIN runs r ON r.run_id=n.run_id "
        "LEFT JOIN decisions d ON d.run_id=n.run_id AND d.node_id=n.node_id "
        "WHERE n.run_id=? ORDER BY n.level,n.path,n.node_id", (run_id,)
    )]


STATE_LABELS = {
    "ELIMINATED": "淘汰",
    "OPPORTUNITY": "末端机会",
    "EXPANDED": "已展开",
    "POLICY_PRUNED": "低体量观察（未下钻到底）",
    "SHORT_HISTORY": "短历史",
    "DEPTH_LIMIT": "深度上限",
    "SUSPENDED": "暂停",
    "PENDING_TREND": "待处理",
    "WAITING_TREND": "待处理",
    "READY_TO_EVALUATE": "待处理",
    "WAITING_TREE": "待下钻",
}


class _SafeHtml(str):
    """Marker for small HTML fragments assembled only from escaped values."""


def _result_label(row: dict) -> str:
    if row.get("is_root"):
        return "扫描起点 · 已展开" if row["state"] == "EXPANDED" else "扫描起点 · 待展开"
    label = STATE_LABELS.get(row["state"], row["state"])
    detail = row.get("result") or row.get("error_reason") or ""
    return f"{label} · {detail}" if detail and detail not in label else label


def _renderer_data(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    processed, opportunities, candidates = [], [], []
    for row in rows:
        base = {
            "id": row["node_id"], "path": row["path"], "level": int(row["level"]),
            "sales_slope": row.get("sales_slope"), "price_slope": row.get("price_slope"),
            "yoy_sales": row.get("yoy_sales"), "yoy_price": row.get("yoy_price"),
            # v4 的体量口径固定为最近 6 个完整连续月，不回退到全期均值。
            "avg_sales": row.get("current_scale"), "avg_price": row.get("avg_price"),
        }
        processed.append({**base, "result": _result_label(row)})
        opportunity = {
            **base, "rule": row.get("direction") or "",
            "opp_level": row.get("opportunity_level") or "",
        }
        if row["state"] == "OPPORTUNITY":
            opportunities.append(opportunity)
        elif row.get("opportunity_level") == "LOW_VOLUME_GROWTH_CANDIDATE":
            candidates.append(opportunity)
    return opportunities, processed, candidates


def _fmt_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, _SafeHtml):
        return str(value)
    return escape(str(value))


def _table(headers: list[str], rows: list[list[Any]], empty: str = "暂无数据") -> str:
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    if rows:
        body = "".join("<tr>" + "".join(f"<td>{_fmt_cell(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    else:
        body = f'<tr><td colspan="{len(headers)}" class="empty-state">{escape(empty)}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _states(states: dict) -> str:
    return _SafeHtml("".join(
        f'<span class="state-chip">{escape(STATE_LABELS.get(key, key))}: {int(value)}</span>'
        for key, value in sorted(states.items())
    ) or "—")


def _render_scan_insights(insight: dict, site: str, extra_html: str = "") -> str:
    coverage = insight["coverage"]
    cards = [
        ("发现节点", coverage["discovered"]), ("父子关系", coverage["edges"]),
        ("正式机会", coverage["opportunities"]), ("低体量剪枝", coverage["policy_pruned"]),
        ("短历史", coverage["short_history"]), ("L4+", coverage["l4_plus"]),
        ("暂停", coverage["suspended"]), ("未知后代", coverage["unknown_descendants"]),
    ]
    card_html = "".join(
        f'<div class="stat-card stat-blue"><div class="num">{value}</div><div class="label">{escape(label)}</div></div>'
        for label, value in cards
    )
    funnel = _table(
        ["层级", "节点数", "状态分布"],
        [[f"L{row['level']}", row["total"], _states(row["states"])] for row in insight["level_funnel"]],
    )
    branches = _table(
        ["父节点", "直接子节点", "状态分布"],
        [[row["parent_id"], row["children"], _states(row["states"])] for row in insight["major_branches"]],
    )
    frontier_rows = []
    for row in insight["opportunity_frontier"]:
        link = escape(_bs_link(site, row["node_id"]), quote=True)
        node = escape(str(row["node_id"]))
        frontier_rows.append([_SafeHtml(f'<a class="node-link" href="{link}" target="_blank" rel="noopener noreferrer">{node}</a>'), row["path"], f"L{row['level']}", row["current_scale"]])
    frontier = _table(["Node / BS榜单", "路径", "层级", "近6月均销"], frontier_rows)
    pruning = _table(
        ["节点", "路径", "近6月均销", "结论"],
        [[r["node_id"], r["path"], r["current_scale"], r["result"]] for r in insight["policy_pruning"]],
    )
    short_history = _table(
        ["节点", "路径", "历史月数", "结论"],
        [[r["node_id"], r["path"], r["months"], r["result"]] for r in insight["short_history"]],
    )
    mcp = insight["mcp_efficiency"]
    rate = "—" if mcp["success_rate"] is None else f"{mcp['success_rate']:.1%}"
    experimental = f'<div class="section"><h2>实验性 M2</h2>{extra_html}</div>' if extra_html else ""
    return f'''<div class="m2-section">
<div class="insight-note">这些数据是扫描过程本身留下的资产，与实时进度使用同一套 SQLite 记录。未知后代表示因低体量剪枝或短历史规则未继续获取，不代表该类目不存在。</div>
<div class="stats">{card_html}</div>
<div class="section"><h2>逐层扫描漏斗</h2>{funnel}</div>
<div class="section"><h2>主要分支</h2>{branches}</div>
<div class="section"><h2>机会前沿</h2>{frontier}</div>
<div class="section"><h2>低体量停止下钻</h2>{pruning}</div>
<div class="section"><h2>短历史节点</h2>{short_history}</div>
<div class="section"><h2>MCP 调用效率</h2>{_table(["尝试", "成功", "失败", "成功率"], [[mcp['attempts'], mcp['success'], mcp['failed'], rate]])}</div>
{experimental}</div>'''


def _render_audit(metadata: dict) -> str:
    items = "".join(
        f'<div class="audit-item"><dt>{escape(str(key))}</dt><dd>{escape(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))}</dd></div>'
        for key, value in metadata.items()
    )
    return f'<div class="section"><h2>本次运行说明</h2><dl class="audit-grid">{items}</dl></div>'


def generate_html(workspace: str, run_id: str, mode: str,
                  output_dir: str, m2_html: str = "") -> str:
    mode = mode.lower()
    if mode not in {"partial", "final"}: raise ValueError("mode 只能是 partial 或 final")
    current = status(workspace, run_id)
    if mode == "final" and not current["completion_ok"]:
        raise RuntimeError(f"禁止生成最终报告：status={current['status']}")
    conn = connect(workspace)
    try:
        run, rows = require_run(conn, run_id), _node_rows(conn, run_id)
        insight = scan_insights(conn, run_id)
        generated = utc_now(); policy = json.loads(run["policy_json"])
        policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        months = conn.execute("SELECT MIN(month),MAX(month) FROM observations WHERE run_id=?", (run_id,)).fetchone()
        metadata = {
            "操作人": run["operator_name"], "站点": run["site"], "根节点": run["root_node_id"],
            "类目": run["category"], "开始": run["created_at"], "完成": run["completed_at"],
            "生成": generated, "时区": run["timezone"], "数据期": f"{months[0] or '-'} 至 {months[1] or '-'}",
            "报告模式": mode, "完成闸门": current["completion_ok"], "策略": run["policy_version"],
            "策略参数": policy, "策略哈希": policy_hash, "应用版本": __version__,
            "数据库结构": SCHEMA_VERSION, "Run ID": run_id, "MCP尝试": current["mcp_attempts"],
            "来源": run["origin"], "原操作人": run["source_user_name"] or run["operator_name"],
        }
        opportunities, processed, candidates = _renderer_data(rows)
        html = render_report(
            opportunities, processed, candidates=candidates,
            m2_html=_render_scan_insights(insight, run["site"], m2_html),
            audit_html=_render_audit(metadata), site=run["site"], category=run["category"],
            init_node=run["root_node_id"], mcp_count=current["mcp_attempts"],
            report_mode=mode, data_dir=workspace,
            storage_key=f"xlj:favorites:{run['site']}:{run_id}",
            volume_threshold=float(policy.get("min_avg_sales", 10000)),
        )
        validate_report_contract(html)
        output_dir = output_dir or run["report_dir"] or os.path.join(os.path.expanduser("~"), "Documents", "WorkBuddy", "寻龙诀v4工作区", "报告")
        os.makedirs(output_dir, exist_ok=True)
        suffix = "partial" if mode == "partial" else "final"
        path = os.path.abspath(os.path.join(output_dir, f"{run['site']}_{_safe_name(run['category'])}_{run_id[:8]}_{suffix}.html"))
        with open(path + ".tmp", "w", encoding="utf-8") as handle: handle.write(html)
        os.replace(path + ".tmp", path)
        conn.execute("INSERT OR REPLACE INTO run_artifacts VALUES(?,?,?,?,?)", (run_id, "html", path, hashlib.sha256(html.encode()).hexdigest(), generated)); conn.commit()
        return path
    finally:
        conn.close()


def regenerate_reports(workspace: str, output_dir: str = "", include_running: bool = False) -> dict:
    """Regenerate historical reports from SQLite without calling MCP again."""
    generated, skipped, errors = [], [], []
    for run in list_runs(workspace):
        run_id, run_status = run["run_id"], run["status"]
        if run_status == "ALL_DONE":
            mode = "final"
        elif run_status == "PARTIAL_DONE" or include_running:
            mode = "partial"
        else:
            skipped.append({"run_id": run_id, "status": run_status, "reason": "运行未结束"})
            continue
        try:
            path = generate_html(workspace, run_id, mode, output_dir)
            generated.append({"run_id": run_id, "status": run_status, "mode": mode, "path": path})
        except (ValueError, RuntimeError, OSError) as exc:
            errors.append({"run_id": run_id, "status": run_status, "error": str(exc)})
    return {"generated": generated, "skipped": skipped, "errors": errors}


def _sheet(wb: Workbook, title: str, headers: List[str], rows: List[List[Any]]) -> None:
    ws = wb.create_sheet(title); ws.append(headers)
    for row in rows: ws.append(row)
    fill, font = PatternFill("solid", fgColor="2F5496"), Font(color="FFFFFF", bold=True)
    for cell in ws[1]: cell.fill, cell.font = fill, font
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions


def export_workbook(workspace: str, run_id: str, output_dir: str) -> str:
    conn = connect(workspace)
    try:
        run, wb, summary = require_run(conn, run_id), Workbook(), status(workspace, run_id); wb.remove(wb.active)
        _sheet(wb, "运行摘要", ["字段", "值"], [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict,list)) else v] for k,v in summary.items()])
        rows = _node_rows(conn, run_id)
        headers = ["类目ID","完整路径","层级","状态","处理结果","错误原因","历史月数","近6月均销","判定方向","回归销量斜率(%)","回归均价斜率(%)","YoY销量增长率(%)","YoY均价增长率(%)","全期平均销量","全期平均价格","销量R²","价格R²","保守投影销量","标签","理由"]
        _sheet(wb, "节点结果", headers, [[r.get(k) for k in ("node_id","path","level","state","result","error_reason","history_months","current_scale","direction","sales_slope","price_slope","yoy_sales","yoy_price","avg_sales","avg_price","sales_r2","price_r2","projected_scale","opportunity_level","reason")] for r in rows])
        _sheet(wb, "机会类目", headers, [[r.get(k) for k in ("node_id","path","level","state","result","error_reason","history_months","current_scale","direction","sales_slope","price_slope","yoy_sales","yoy_price","avg_sales","avg_price","sales_r2","price_r2","projected_scale","opportunity_level","reason")] for r in rows if r["state"]=="OPPORTUNITY"])
        obs = [list(r) for r in conn.execute("SELECT node_id,indicator,month,value,created_at FROM observations WHERE run_id=? ORDER BY node_id,indicator,month",(run_id,))]
        _sheet(wb,"月度数据",["类目ID","指标","月份","数值","写入时间"],obs)
        attempts=[list(r) for r in conn.execute("SELECT attempt_id,job_id,node_id,site,call_type,status,error,data_count,attempt_no,created_at FROM mcp_attempts WHERE run_id=? ORDER BY attempt_id",(run_id,))]
        _sheet(wb,"MCP日志",["序号","任务ID","类目ID","站点","调用类型","状态","错误","数据条数","尝试序号","时间"],attempts)
        events=[list(r) for r in conn.execute("SELECT event_id,event_type,node_id,detail,created_at FROM events WHERE run_id=? ORDER BY event_id",(run_id,))]
        _sheet(wb,"事件日志",["序号","事件","类目ID","详情","时间"],events)
        insight=scan_insights(conn,run_id); _sheet(wb,"扫描过程洞察",["模块","JSON"],[[k,json.dumps(v,ensure_ascii=False)] for k,v in insight.items()])
        output_dir = output_dir or run["report_dir"] or os.path.join(os.path.expanduser("~"), "Documents", "WorkBuddy", "寻龙诀v4工作区", "报告")
        os.makedirs(output_dir,exist_ok=True); path=os.path.abspath(os.path.join(output_dir,f"{run['site']}_{_safe_name(run['category'])}_{run_id[:8]}_summary.xlsx")); wb.save(path); wb.close(); return path
    finally: conn.close()


def generate_m2(workspace: str, run_id: str, output_dir: str) -> str:
    """Compatibility alias: M2 is now the scan-insight tab in the main report."""
    current = status(workspace, run_id)
    return generate_html(workspace, run_id, "final" if current["completion_ok"] else "partial", output_dir)
