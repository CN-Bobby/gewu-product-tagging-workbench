"""Read-only v3 Excel scanner and explicit SQLite importer."""
from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple

from openpyxl import load_workbook

from .db import connect, event, require_run, transaction, utc_now
from .engine import (
    POLICY_VERSION, _ensure_job, _ensure_trend_jobs, _run_status, create_run,
)
from .site_profiles import normalize_site

FILES = {
    "tasks": "01_待办任务栈.xlsx",
    "mcp": "02_MCP返回数据.xlsx",
    "processed": "03_已处理节点表.xlsx",
    "opportunities": "04_机会类目结果表.xlsx",
    "log": "05_运行日志.xlsx",
}


def _sheet_records(path: str, sheet: str | None = None) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        values = ws.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(values, ())]
        records = []
        for row in values:
            record = {
                headers[index]: value
                for index, value in enumerate(row[:len(headers)])
                if headers[index]
            }
            if any(value is not None and str(value).strip() for value in record.values()):
                records.append(record)
        return records
    finally:
        wb.close()


def _value(row: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _node_id(row: Dict[str, Any], *names: str) -> str:
    value = str(_value(row, *names, default="") or "").strip()
    return value if value.isdigit() else ""


def scan_v3(source: str, site: str) -> Dict[str, Any]:
    source = os.path.abspath(source)
    site = normalize_site(site)
    found = {
        key: os.path.exists(os.path.join(source, name))
        for key, name in FILES.items()
    }
    tasks = _sheet_records(os.path.join(source, FILES["tasks"]))
    processed = _sheet_records(os.path.join(source, FILES["processed"]))
    opportunities = _sheet_records(os.path.join(source, FILES["opportunities"]))
    sales = _sheet_records(os.path.join(source, FILES["mcp"]), "量价数据表")
    subcats = _sheet_records(os.path.join(source, FILES["mcp"]), "子类目数据表")
    mcp_logs = _sheet_records(os.path.join(source, FILES["mcp"]), "数据获取日志表")
    filtered = {}
    for name, rows in {
        "tasks": tasks, "processed": processed, "opportunities": opportunities,
        "sales": sales, "subcategories": subcats, "mcp_logs": mcp_logs,
    }.items():
        filtered[name] = [
            row for row in rows
            if normalize_site(_value(row, "站点", default=site)) == site
        ]
    terminal = defaultdict(set)
    for row in filtered["processed"]:
        node_id = _node_id(row, "类目ID", "节点ID")
        result = str(_value(row, "处理结果", default="") or "")
        if not node_id:
            continue
        if "淘汰" in result:
            terminal[node_id].add("ELIMINATED")
        elif "深度上限" in result:
            terminal[node_id].add("DEPTH_LIMIT")
        elif "末端" in result:
            terminal[node_id].add("OPPORTUNITY")
    conflicts = [
        {"node_id": node_id, "terminal_states": sorted(states)}
        for node_id, states in terminal.items() if len(states) > 1
    ]
    return {
        "source": source,
        "site": site,
        "files": found,
        "counts": {name: len(rows) for name, rows in filtered.items()},
        "conflicts": conflicts,
        "can_commit": found["tasks"] or found["processed"] or found["mcp"],
        "_records": filtered,
    }


def _state_from_task(status: str) -> Tuple[str, str]:
    if status == "异常挂起":
        return "SUSPENDED", "IMPORTED_SUSPENDED"
    if status == "待获取子类目":
        return "WAITING_TREE", "TREND_PASSED"
    return "PENDING_TREND", ""


def _state_from_processed(result: str) -> Tuple[str, str]:
    if "淘汰" in result:
        return "ELIMINATED", "ELIMINATED"
    if "深度上限" in result:
        return "DEPTH_LIMIT", "DEPTH_LIMIT"
    if "低潜力末端" in result:
        return "OPPORTUNITY", "LOW_POTENTIAL_STOP"
    if "末端" in result:
        return "OPPORTUNITY", "LEAF_OPPORTUNITY"
    if "待下钻" in result:
        return "WAITING_TREE", "TREND_PASSED"
    return "PENDING_TREND", ""


def import_v3(workspace: str, source: str, site: str, root_node_id: str,
              category: str, *, commit: bool = False) -> Dict[str, Any]:
    report = scan_v3(source, site)
    public_report = {key: value for key, value in report.items() if key != "_records"}
    public_report["committed"] = False
    if not commit:
        return public_report
    if not report["can_commit"]:
        raise ValueError("没有发现可导入的v3工作区文件")
    site = normalize_site(site)
    root_node_id = str(root_node_id or "").strip()
    if not root_node_id.isdigit():
        raise ValueError("导入时必须提供纯数字根Node ID")
    records = report["_records"]
    run_id = create_run(
        workspace, site, root_node_id, category, create_root_job=False,
    )
    conn = connect(workspace)
    try:
        with transaction(conn):
            run = require_run(conn, run_id)
            now = utc_now()
            nodes: Dict[str, Dict[str, Any]] = {
                root_node_id: {
                    "path": category, "level": 1, "state": "WAITING_TREE",
                    "result": "", "error": "",
                }
            }
            conflicts = {item["node_id"] for item in report["conflicts"]}
            for row in records["tasks"]:
                node_id = _node_id(row, "类目ID", "节点ID")
                if not node_id:
                    continue
                state, result = _state_from_task(
                    str(_value(row, "状态", default="") or "")
                )
                nodes[node_id] = {
                    "path": str(_value(row, "完整路径", "路径", default=node_id)),
                    "level": int(_value(row, "层级", default=1) or 1),
                    "state": state, "result": result,
                    "error": str(_value(row, "异常原因", default="") or ""),
                }
            decision_rows: Dict[str, Dict[str, Any]] = {}
            for row in records["processed"]:
                node_id = _node_id(row, "类目ID", "节点ID")
                if not node_id:
                    continue
                state, result = _state_from_processed(
                    str(_value(row, "处理结果", default="") or "")
                )
                current = nodes.get(node_id, {})
                nodes[node_id] = {
                    "path": str(_value(
                        row, "完整路径", "路径", default=current.get("path", node_id)
                    )),
                    "level": int(_value(
                        row, "层级", default=current.get("level", 1)
                    ) or 1),
                    "state": "SUSPENDED" if node_id in conflicts else state,
                    "result": "MIGRATION_CONFLICT" if node_id in conflicts else result,
                    "error": (
                        "v3存在冲突终态" if node_id in conflicts
                        else current.get("error", "")
                    ),
                }
                decision_rows[node_id] = row
            opportunity_rows = {
                _node_id(row, "类目ID", "节点ID"): row
                for row in records["opportunities"]
                if _node_id(row, "类目ID", "节点ID")
            }
            edges = []
            tree_parents = defaultdict(list)
            leaf_parents = set()
            for row in records["subcategories"]:
                parent_id = _node_id(row, "父类目ID")
                child_raw = str(_value(row, "子类目ID", default="") or "").strip()
                if not parent_id:
                    continue
                if child_raw == "__LEAF__":
                    leaf_parents.add(parent_id)
                    continue
                if not child_raw.isdigit():
                    continue
                name = str(_value(row, "子类目名称", default=child_raw) or child_raw)
                edges.append((parent_id, child_raw, name))
                tree_parents[parent_id].append(child_raw)
                if child_raw not in nodes:
                    parent = nodes.get(parent_id, {
                        "path": category, "level": 1,
                    })
                    nodes[child_raw] = {
                        "path": f"{parent['path']}/{name}",
                        "level": int(parent["level"]) + 1,
                        "state": "PENDING_TREND", "result": "", "error": "",
                    }
            for parent_id in set(tree_parents) | leaf_parents:
                if parent_id not in nodes:
                    nodes[parent_id] = {
                        "path": parent_id, "level": 1,
                        "state": "OPPORTUNITY", "result": "ROOT_EXPANDED",
                        "error": "",
                    }
                elif nodes[parent_id]["state"] not in {
                    "ELIMINATED", "DEPTH_LIMIT", "SUSPENDED",
                }:
                    nodes[parent_id]["state"] = "OPPORTUNITY"
                    if parent_id == root_node_id:
                        nodes[parent_id]["result"] = (
                            "ROOT_EXPANDED" if tree_parents[parent_id] else "ROOT_LEAF"
                        )
                    elif parent_id in leaf_parents:
                        nodes[parent_id]["result"] = "LEAF_OPPORTUNITY"
                    else:
                        nodes[parent_id]["result"] = "EXPANDED"
            conn.execute("DELETE FROM nodes WHERE run_id=?", (run_id,))
            for node_id, item in nodes.items():
                conn.execute(
                    "INSERT INTO nodes("
                    "run_id,node_id,path,level,state,result,error_reason,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, node_id, item["path"], item["level"], item["state"],
                        item["result"], item["error"], now, now,
                    ),
                )
            for parent_id, child_id, name in edges:
                conn.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                    (run_id, parent_id, child_id, name, now),
                )
            for row in records["sales"]:
                node_id = _node_id(row, "类目ID", "节点ID")
                month = str(_value(row, "月份", default="") or "").strip()
                if not node_id or node_id not in nodes or not month:
                    continue
                for indicator, header in (
                    ("SalesCount", "月销量"), ("AvgPrice", "月均价"),
                ):
                    value = _value(row, header)
                    if value is None:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)",
                        (run_id, node_id, indicator, month, float(value), now),
                    )
            for node_id, row in decision_rows.items():
                opp_row = opportunity_rows.get(node_id, {})
                status_value = (
                    "eliminated" if nodes[node_id]["state"] == "ELIMINATED"
                    else "passed"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO decisions VALUES("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, node_id, status_value,
                        _value(opp_row, "通过规则", default="") or "",
                        _value(row, "销量年化斜率(%)"),
                        _value(row, "均价年化斜率(%)"),
                        _value(row, "YoY销量增长率(%)"),
                        _value(row, "YoY均价增长率(%)"),
                        _value(row, "平均月销量"),
                        _value(row, "平均月均价"),
                        None, None,
                        _value(opp_row, "机会级别", default="") or "",
                        "从v3工作区导入", POLICY_VERSION, now,
                    ),
                )
            for row in records["mcp_logs"]:
                node_id = _node_id(row, "类目ID", "节点ID") or root_node_id
                conn.execute(
                    "INSERT INTO mcp_attempts("
                    "run_id,job_id,node_id,site,call_type,status,error,data_count,created_at"
                    ") VALUES(?,NULL,?,?,?,?,?,?,?)",
                    (
                        run_id, node_id, site,
                        str(_value(row, "调用类型", default="legacy-v3")),
                        str(_value(row, "返回状态", default="成功")),
                        str(_value(row, "失败原因", default="") or ""),
                        int(_value(row, "数据条数", default=0) or 0),
                        str(_value(row, "调用时间", default=now) or now),
                    ),
                )
            for node_id, item in nodes.items():
                if item["state"] in {"PENDING_TREND", "WAITING_TREND"}:
                    _ensure_trend_jobs(conn, run_id, node_id)
                    for indicator in ("SalesCount", "AvgPrice"):
                        count = conn.execute(
                            "SELECT COUNT(*) FROM observations WHERE run_id=? "
                            "AND node_id=? AND indicator=?",
                            (run_id, node_id, indicator),
                        ).fetchone()[0]
                        if count >= 24:
                            conn.execute(
                                "UPDATE jobs SET state='SUCCEEDED',attempts=1,"
                                "updated_at=? WHERE run_id=? AND node_id=? "
                                "AND job_type='category_trend' AND indicator=?",
                                (now, run_id, node_id, indicator),
                            )
                elif item["state"] == "WAITING_TREE":
                    _ensure_job(conn, run_id, node_id, "category_tree")
            run_status = _run_status(conn, run_id)
            event(conn, run_id, "V3_IMPORT_COMMITTED", detail={
                "source": os.path.abspath(source),
                "counts": report["counts"],
                "conflicts": report["conflicts"],
                "status": run_status,
            })
        public_report.update({
            "committed": True,
            "run_id": run_id,
            "run_status": run_status,
        })
        return public_report
    except Exception:
        # create_run committed an empty run before the import transaction.
        with transaction(conn):
            conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        raise
    finally:
        conn.close()

