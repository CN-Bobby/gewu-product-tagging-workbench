"""Command line interface for WorkBuddy and human operators."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
from typing import Any

from . import __version__
from .db import SCHEMA_VERSION, connect, database_path, default_data_dir, default_report_dir
from .engine import (DEFAULT_POLICY, PROTOCOL_VERSION, create_run, get_owner,
                     ingest_batch, ingest_results, lease_jobs, list_batches,
                     list_runs, pulse_batch, resume_run, start_run, status, takeover_run)
from .ledger import claim_request
from .outputs import export_workbook, generate_html, generate_m2, regenerate_reports
from .panel import open_panel, panel_status, serve_panel, stop_panel
from .syncpack import export_pack, import_pack


_RESPONSE_FILE: str | None = None


class _ResponseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if _RESPONSE_FILE:
            try:
                _write_json(_RESPONSE_FILE, {"ok": False, "error": message, "exit_code": 2})
            except OSError:
                pass
        super().error(message)


def _workspace(args) -> str:
    return os.path.abspath(getattr(args, "workspace", None) or os.environ.get("XUNLONGJUE_DATA_DIR") or default_data_dir())


def _write_json(path: str, value: Any) -> str:
    target = os.path.abspath(path); os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".tmp", "w", encoding="utf-8") as handle: json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(target + ".tmp", target); return target


def _print(value: Any) -> None:
    if _RESPONSE_FILE:
        _write_json(_RESPONSE_FILE, value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _response_arg(parser) -> None:
    parser.add_argument(
        "--response-file",
        help="原子写入 UTF-8 JSON 回执；供无法捕获 stdout 的客户端读取",
    )


def _policy_args(parser) -> None:
    parser.add_argument(
        "--volume-threshold", "--min-avg-sales", dest="volume_threshold",
        type=float, default=DEFAULT_POLICY["min_avg_sales"],
    )
    parser.add_argument("--max-level", type=int, default=DEFAULT_POLICY["max_level"])
    parser.add_argument("--sales-decline-tolerance", type=float, default=DEFAULT_POLICY["sales_decline_tolerance"])
    parser.add_argument("--price-decline-tolerance", type=float, default=DEFAULT_POLICY["price_decline_tolerance"])
    parser.add_argument("--min-r-squared", type=float, default=DEFAULT_POLICY["min_r_squared"])
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_POLICY["max_attempts"])
    parser.add_argument("--max-data-lag-months", type=int, default=DEFAULT_POLICY["max_data_lag_months"])
    parser.add_argument("--batch-size", type=int, default=DEFAULT_POLICY["batch_size"])


def build_parser() -> argparse.ArgumentParser:
    parser = _ResponseArgumentParser(prog="xlj", description="寻龙诀 v4.2 SQLite 状态机")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="创建新运行"); init.add_argument("--site", required=True); init.add_argument("--root-node", required=True); init.add_argument("--category", required=True); init.add_argument("--operator", default=""); init.add_argument("--workspace"); _policy_args(init)
    start = sub.add_parser("start", help="自然语言入口：创建或复用同参数的活动运行"); start.add_argument("--site", required=True); start.add_argument("--root-node", required=True); start.add_argument("--category", required=True); start.add_argument("--operator", default=""); start.add_argument("--report-dir", default=""); start.add_argument("--new-run", action="store_true"); start.add_argument("--workspace"); _policy_args(start)
    claim = sub.add_parser("claim", help="领取面板生成的任务码"); claim.add_argument("--code", required=True); claim.add_argument("--workspace")
    nxt = sub.add_parser("next", help="领取下一批MCP任务并生成托管IPC目录"); nxt.add_argument("--run", required=True); nxt.add_argument("--limit", type=int, default=20); nxt.add_argument("--worker", default=""); nxt.add_argument("--output"); nxt.add_argument("--workspace")
    ing = sub.add_parser("ingest", help="原子封存并回写整个MCP批次"); ing.add_argument("--run", required=True); ing.add_argument("--batch"); ing.add_argument("--worker", default=""); ing.add_argument("--input"); ing.add_argument("--workspace")
    pulse = sub.add_parser("pulse", help="更新批次可视进度，不写业务结果"); pulse.add_argument("--run", required=True); pulse.add_argument("--batch", required=True); pulse.add_argument("--completed", type=int, default=0); pulse.add_argument("--failed", type=int, default=0); pulse.add_argument("--current-node", default=""); pulse.add_argument("--stage", default="CALLING_MCP"); pulse.add_argument("--workspace")
    batches = sub.add_parser("batches", help="查看Run的批次和租约"); batches.add_argument("--run", required=True); batches.add_argument("--json", action="store_true"); batches.add_argument("--workspace")
    takeover = sub.add_parser("takeover", help="由新对话框接管Run"); takeover.add_argument("--run", required=True); takeover.add_argument("--force", action="store_true"); takeover.add_argument("--label", default="WorkBuddy/接管"); takeover.add_argument("--workspace")
    stat = sub.add_parser("status", help="查看运行状态和扫描洞察"); stat.add_argument("--run", required=True); stat.add_argument("--json", action="store_true"); stat.add_argument("--workspace")
    runs = sub.add_parser("runs", help="列出本版台账"); runs.add_argument("--json", action="store_true"); runs.add_argument("--workspace")
    resume = sub.add_parser("resume", help="恢复暂停节点"); resume.add_argument("--run", required=True); resume.add_argument("--all-suspended", action="store_true"); resume.add_argument("--workspace")
    report = sub.add_parser("report", help="生成主HTML报告"); report.add_argument("--run", required=True); report.add_argument("--mode", choices=("partial","final"), required=True); report.add_argument("--output"); report.add_argument("--workspace")
    report_all = sub.add_parser("report-all", help="从SQLite批量重新生成历史Run报告（不调用MCP）"); report_all.add_argument("--output"); report_all.add_argument("--include-running", action="store_true"); report_all.add_argument("--workspace")
    export = sub.add_parser("export", help="导出汇总Excel"); export.add_argument("--run", required=True); export.add_argument("--output"); export.add_argument("--workspace")
    panel = sub.add_parser("panel", help="管理本地参数和台账面板")
    panel.add_argument("--workspace"); panel.add_argument("--host", default="127.0.0.1"); panel.add_argument("--port", type=int, default=8765); panel.add_argument("--no-browser", action="store_true")
    panel_sub = panel.add_subparsers(dest="panel_command")
    for action in ("open", "serve"):
        command = panel_sub.add_parser(action)
        command.add_argument("--workspace"); command.add_argument("--host", default="127.0.0.1"); command.add_argument("--port", type=int, default=8765)
        if action == "open":
            command.add_argument("--no-browser", action="store_true"); command.add_argument("--timeout", type=float, default=8.0)
        else:
            command.add_argument("--token", default="")
    for action in ("status", "stop"):
        command = panel_sub.add_parser(action); command.add_argument("--workspace")
    sync = sub.add_parser("sync", help="跨设备手工包"); sync_sub = sync.add_subparsers(dest="sync_command", required=True)
    sx = sync_sub.add_parser("export"); sx.add_argument("--output", required=True); sx.add_argument("--run", action="append"); sx.add_argument("--workspace")
    si = sync_sub.add_parser("import"); si.add_argument("--input", required=True); si.add_argument("--commit", action="store_true"); si.add_argument("--workspace")
    doctor = sub.add_parser("doctor", help="检查运行环境和数据库迁移"); doctor.add_argument("--workspace"); doctor.add_argument("--json", action="store_true")
    m2 = sub.add_parser("m2", help="兼容命令：生成带扫描洞察的主报告"); m2.add_argument("--run", required=True); m2.add_argument("--output", default=default_report_dir()); m2.add_argument("--workspace")
    for command in (init, start, claim, nxt, ing, pulse, batches, takeover, stat,
                    runs, resume, report, report_all, export, doctor, m2):
        _response_arg(command)
    return parser


def _chosen_policy(args) -> dict:
    return {"min_avg_sales": args.volume_threshold, "max_level": args.max_level,
            "sales_decline_tolerance": args.sales_decline_tolerance,
            "price_decline_tolerance": args.price_decline_tolerance,
            "min_r_squared": args.min_r_squared, "max_attempts": args.max_attempts,
            "max_data_lag_months": args.max_data_lag_months, "batch_size": args.batch_size}


def main(argv: list[str] | None = None) -> int:
    global _RESPONSE_FILE
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    _RESPONSE_FILE = None
    if "--response-file" in raw_argv:
        index = raw_argv.index("--response-file")
        if index + 1 < len(raw_argv):
            _RESPONSE_FILE = raw_argv[index + 1]
    args = build_parser().parse_args(raw_argv)
    _RESPONSE_FILE = getattr(args, "response_file", None)
    try:
        workspace = _workspace(args)
        if args.command == "init":
            run_id=create_run(workspace,args.site,args.root_node,args.category,policy=_chosen_policy(args),operator_name=args.operator,report_dir=default_report_dir())
            owner=get_owner(workspace,run_id)
            _print({"schema_version":PROTOCOL_VERSION,"run_id":run_id,"worker_id":owner["worker_id"],"data_dir":workspace,"database":database_path(workspace),"status":"RUNNING"}); return 0
        if args.command == "start":
            value=start_run(workspace,args.site,args.root_node,args.category,policy=_chosen_policy(args),operator_name=args.operator,report_dir=args.report_dir or default_report_dir(),force_new=args.new_run)
            value.update({"schema_version":PROTOCOL_VERSION,"data_dir":workspace,"database":database_path(workspace)})
            _print(value); return 0
        if args.command == "claim": _print(claim_request(workspace,args.code)); return 0
        if args.command == "next":
            if args.output: raise ValueError("协议 v3 已使用托管IPC路径，请删除 --output")
            _print(lease_jobs(workspace,args.run,args.limit,args.worker)); return 0
        if args.command == "ingest":
            if args.batch:
                if args.input: raise ValueError("协议 v3 请使用 --batch，不要同时传入 --input")
                _print(ingest_batch(workspace,args.run,args.batch,args.worker)); return 0
            if not args.input: raise ValueError("必须提供 --batch；--input 仅兼容升级前已租出的 v2 任务")
            with open(args.input,"r",encoding="utf-8-sig") as handle: doc=json.load(handle)
            _print(ingest_results(workspace,args.run,doc,worker_id=args.worker)); return 0
        if args.command == "pulse":
            _print(pulse_batch(workspace,args.run,args.batch,completed=args.completed,failed=args.failed,current_node_id=args.current_node,stage=args.stage)); return 0
        if args.command == "batches":
            value=list_batches(workspace,args.run); _print(value) if args.json else [print(f"{r['batch_id']} {r['state']} {r['job_count']} {r['lease_until']}") for r in value]; return 0
        if args.command == "takeover": _print(takeover_run(workspace,args.run,args.force,args.label)); return 0
        if args.command == "status":
            value=status(workspace,args.run)
            if args.json: _print(value)
            else: print(f"运行: {value['run_id']}\n状态: {value['status']}\n节点: {value['node_counts']}\nMCP尝试: {value['mcp_attempts']}\n层级漏斗: {value['insights']['level_funnel']}")
            return 0
        if args.command == "runs":
            value=list_runs(workspace); _print(value) if args.json else [print(f"{r['run_id']} {r['site']} {r['status']} {r['category']} {r['operator_name']}") for r in value]; return 0
        if args.command == "resume": _print(resume_run(workspace,args.run,args.all_suspended)); return 0
        if args.command == "report": _print({"report":generate_html(workspace,args.run,args.mode,args.output)}); return 0
        if args.command == "report-all": _print(regenerate_reports(workspace,args.output,args.include_running)); return 0
        if args.command == "export": _print({"workbook":export_workbook(workspace,args.run,args.output)}); return 0
        if args.command == "panel":
            action = args.panel_command or "open"
            if action == "open": _print(open_panel(workspace,host=args.host,port=args.port,open_browser=not args.no_browser,timeout=getattr(args,"timeout",8.0)))
            elif action == "serve": serve_panel(workspace,args.host,args.port,getattr(args,"token",""))
            elif action == "status": _print(panel_status(workspace))
            elif action == "stop": _print(stop_panel(workspace))
            return 0
        if args.command == "sync":
            _print({"package":export_pack(workspace,args.output,args.run)}) if args.sync_command=="export" else _print(import_pack(workspace,args.input,args.commit)); return 0
        if args.command == "doctor":
            conn=connect(workspace)
            try: journal=conn.execute("PRAGMA journal_mode").fetchone()[0]; integrity=conn.execute("PRAGMA integrity_check").fetchone()[0]; migrations=[r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            finally: conn.close()
            value={"ok":integrity=="ok" and migrations[-1]==SCHEMA_VERSION,"version":__version__,"python":sys.version.split()[0],"platform":platform.platform(),"data_dir":workspace,"database":database_path(workspace),"sqlite":sqlite3.sqlite_version,"journal_mode":journal,"integrity_check":integrity,"schema_migrations":migrations}
            _print(value) if args.json else [print(f"{k}: {v}") for k,v in value.items()]; return 0 if value["ok"] else 2
        if args.command == "m2": _print({"report":generate_m2(workspace,args.run,args.output),"note":"M2已并入扫描过程洞察"}); return 0
    except (ValueError,RuntimeError,OSError,json.JSONDecodeError,sqlite3.Error) as exc:
        if _RESPONSE_FILE:
            _write_json(_RESPONSE_FILE, {"ok": False, "error": str(exc), "exit_code": 2})
        print(f"[xlj] {exc}",file=sys.stderr); return 2
    return 1
