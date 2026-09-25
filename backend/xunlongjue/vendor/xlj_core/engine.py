"""Deterministic SQLite state machine for Xunlongjue v4.2."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import connect, event, require_run, transaction, utc_now
from .site_profiles import normalize_site
from .trend_judgment import judge_category_trend

PROTOCOL_VERSION = "3"
LEGACY_PROTOCOL_VERSION = "2"
POLICY_VERSION = "xlj-v4.1"
LEASE_MINUTES = 30
INDICATORS = {"SalesCount", "AvgPrice"}
JOB_TYPES = {"category_tree", "category_trend"}
TERMINAL_STATES = {
    "ELIMINATED", "OPPORTUNITY", "DEPTH_LIMIT", "POLICY_PRUNED",
    "SHORT_HISTORY", "EXPANDED", "SUSPENDED",
}
ACTIVE_STATES = {
    "PENDING_TREND", "WAITING_TREND", "READY_TO_EVALUATE", "WAITING_TREE",
}
DEFAULT_POLICY = {
    "policy_version": POLICY_VERSION,
    "min_avg_sales": 10000.0,
    "sales_decline_tolerance": 0.05,
    "price_decline_tolerance": 0.10,
    "min_r_squared": 0.15,
    "max_level": 12,
    "max_attempts": 3,
    "max_data_lag_months": 2,
    "batch_size": 20,
}


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


def _validate_node_id(value: object) -> str:
    node_id = str(value or "").strip()
    if not node_id.isdigit():
        raise ValueError(f"Node ID 必须为纯数字，收到: {value!r}")
    return node_id


def _validated_policy(policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    chosen = dict(DEFAULT_POLICY)
    if policy:
        chosen.update(policy)
    chosen["policy_version"] = POLICY_VERSION
    try:
        chosen["min_avg_sales"] = float(chosen["min_avg_sales"])
        chosen["sales_decline_tolerance"] = float(chosen["sales_decline_tolerance"])
        chosen["price_decline_tolerance"] = float(chosen["price_decline_tolerance"])
        chosen["min_r_squared"] = float(chosen["min_r_squared"])
        chosen["max_level"] = int(chosen["max_level"])
        chosen["max_attempts"] = int(chosen["max_attempts"])
        chosen["max_data_lag_months"] = int(chosen["max_data_lag_months"])
        chosen["batch_size"] = int(chosen["batch_size"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("策略参数必须是有效数字") from exc
    numeric = (chosen["min_avg_sales"], chosen["sales_decline_tolerance"],
               chosen["price_decline_tolerance"], chosen["min_r_squared"])
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("策略参数不得为 NaN 或无穷大")
    if chosen["min_avg_sales"] <= 0:
        raise ValueError("体量分级线必须大于 0")
    if not 1 <= chosen["max_level"] <= 100:
        raise ValueError("最大层级必须在 1 至 100 之间")
    if not 1 <= chosen["max_attempts"] <= 10:
        raise ValueError("重试次数必须在 1 至 10 之间")
    if not 0 <= chosen["max_data_lag_months"] <= 24:
        raise ValueError("最大数据滞后月必须在 0 至 24 之间")
    if not 1 <= chosen["batch_size"] <= 100:
        raise ValueError("批量必须在 1 至 100 之间")
    if not 0 <= chosen["min_r_squared"] <= 1:
        raise ValueError("最低 R² 必须在 0 至 1 之间")
    for key, label in (("sales_decline_tolerance", "销量下降容忍"),
                       ("price_decline_tolerance", "价格下降容忍")):
        if not 0 <= chosen[key] <= 1:
            raise ValueError(f"{label}必须在 0 至 1 之间")
    return chosen


def _state(conn, run_id: str, node_id: str, state: str, result: str = "",
           reason: str = "") -> None:
    old = conn.execute(
        "SELECT state FROM nodes WHERE run_id=? AND node_id=?", (run_id, node_id),
    ).fetchone()
    conn.execute(
        "UPDATE nodes SET state=?,result=?,error_reason=?,updated_at=? WHERE run_id=? AND node_id=?",
        (state, result, reason, utc_now(), run_id, node_id),
    )
    if old and old["state"] != state:
        event(conn, run_id, "NODE_STATE_CHANGED", node_id, {
            "from": old["state"], "to": state, "result": result, "reason": reason,
        })


def _ensure_job(conn, run_id: str, node_id: str, job_type: str,
                indicator: str = "") -> None:
    if job_type not in JOB_TYPES or (job_type == "category_trend" and indicator not in INDICATORS):
        raise ValueError("未知任务类型或指标")
    now = utc_now()
    conn.execute(
        "INSERT OR IGNORE INTO jobs(job_id,run_id,node_id,job_type,indicator,state,eligible_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'PENDING',?,?,?)",
        (_new_id(), run_id, node_id, job_type, indicator, now, now, now),
    )


def _ensure_trend_jobs(conn, run_id: str, node_id: str) -> None:
    root = conn.execute("SELECT root_node_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if root and str(root["root_node_id"]) == str(node_id):
        return
    for indicator in ("SalesCount", "AvgPrice"):
        _ensure_job(conn, run_id, node_id, "category_trend", indicator)


def _profile(conn) -> dict:
    row = conn.execute("SELECT * FROM local_profile WHERE profile_id=1").fetchone()
    if row:
        return dict(row)
    user_id, device_id = _new_id(), _new_id()
    name = "本机操作人"
    conn.execute(
        "INSERT INTO local_profile VALUES(1,?,?,?,?,?)",
        (user_id, name, device_id, "Windows设备", utc_now()),
    )
    return {"user_id": user_id, "display_name": name, "device_id": device_id,
            "device_name": "Windows设备"}


def _create_run_in_conn(conn, site: str, root_node_id: str, category: str,
                        *, policy: Optional[Dict[str, Any]] = None,
                        operator_name: str = "", run_id: str = "",
                        report_dir: str = "") -> str:
    site, root_node_id = normalize_site(site), _validate_node_id(root_node_id)
    category = str(category or "").strip()
    if not category:
        raise ValueError("类目名不能为空")
    chosen = _validated_policy(policy)
    run_id, now, owner_id = run_id or _new_id(), utc_now(), _new_id()
    profile = _profile(conn)
    op_name = operator_name.strip() or profile["display_name"]
    conn.execute(
        "INSERT INTO runs(run_id,site,root_node_id,category,policy_version,policy_json,status,"
        "created_at,updated_at,operator_name,operator_id,device_id,timezone,origin,report_dir,"
        "owner_id,owner_label,owner_heartbeat) "
        "VALUES(?,?,?,?,?,?,'RUNNING',?,?,?,?,?,?,'local',?,?,?,?)",
        (run_id, site, root_node_id, category, POLICY_VERSION,
         json.dumps(chosen, ensure_ascii=False, sort_keys=True), now, now,
         op_name, profile["user_id"], profile["device_id"],
         datetime.now().astimezone().tzname() or "local", report_dir,
         owner_id, f"WorkBuddy/{op_name}", now),
    )
    conn.execute(
        "INSERT INTO nodes(run_id,node_id,path,level,state,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (run_id, root_node_id, category, 1, "WAITING_TREE", now, now),
    )
    _ensure_job(conn, run_id, root_node_id, "category_tree")
    event(conn, run_id, "RUN_STARTED", root_node_id, {
        "site": site, "category": category, "policy": chosen,
    })
    return run_id


def create_run(workspace: str, site: str, root_node_id: str, category: str,
               *, policy: Optional[Dict[str, Any]] = None,
               operator_name: str = "", run_id: str = "", report_dir: str = "") -> str:
    conn = connect(workspace)
    try:
        with transaction(conn):
            return _create_run_in_conn(
                conn, site, root_node_id, category, policy=policy,
                operator_name=operator_name, run_id=run_id, report_dir=report_dir,
            )
    finally:
        conn.close()


def start_run(workspace: str, site: str, root_node_id: str, category: str,
              *, policy: Optional[Dict[str, Any]] = None, operator_name: str = "",
              report_dir: str = "", force_new: bool = False) -> Dict[str, Any]:
    """Create a run or idempotently reuse the same active scan.

    This is the user-facing natural-language entry.  It prevents a repeated
    WorkBuddy instruction from silently creating a duplicate run, while still
    allowing an explicit new run for intentional comparisons.
    """
    site, root_node_id = normalize_site(site), _validate_node_id(root_node_id)
    category = str(category or "").strip()
    chosen = _validated_policy(policy)
    policy_json = json.dumps(chosen, ensure_ascii=False, sort_keys=True)
    conn = connect(workspace)
    try:
        with transaction(conn):
            if not force_new:
                row = conn.execute(
                    "SELECT run_id FROM runs WHERE site=? AND root_node_id=? AND category=? "
                    "AND policy_json=? AND status IN ('RUNNING','PARTIAL_DONE') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (site, root_node_id, category, policy_json),
                ).fetchone()
                if row:
                    return {"run_id": row["run_id"], "reused": True, "status": "RUNNING"}
            run_id = _create_run_in_conn(
                conn, site, root_node_id, category, policy=chosen,
                operator_name=operator_name, report_dir=report_dir,
            )
            return {"run_id": run_id, "reused": False, "status": "RUNNING"}
    finally:
        conn.close()


def _ipc_paths(workspace: str, run_id: str, batch_id: str) -> Dict[str, str]:
    root = os.path.abspath(os.path.join(workspace, "ipc"))
    directory = os.path.abspath(os.path.join(root, run_id, batch_id))
    if os.path.commonpath([root, directory]) != root:
        raise ValueError("非法IPC路径")
    return {
        "directory": directory,
        "relative_dir": os.path.relpath(directory, os.path.abspath(workspace)),
        "jobs_path": os.path.join(directory, "jobs.json"),
        "results_draft_path": os.path.join(directory, "results.draft.json"),
        "results_path": os.path.join(directory, "results.json"),
        "receipt_path": os.path.join(directory, "receipt.json"),
    }


def _atomic_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def cleanup_ipc(workspace: str, retention_days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))).isoformat(timespec="seconds")
    conn = connect(workspace)
    removed = 0
    try:
        rows = conn.execute(
            "SELECT run_id,batch_id FROM batches WHERE state IN ('INGESTED','EXPIRED','ABANDONED') "
            "AND updated_at<?", (cutoff,),
        ).fetchall()
        root = os.path.abspath(os.path.join(workspace, "ipc"))
        for row in rows:
            directory = _ipc_paths(workspace, row["run_id"], row["batch_id"])["directory"]
            if os.path.isdir(directory) and os.path.commonpath([root, directory]) == root:
                shutil.rmtree(directory); removed += 1
        return removed
    finally:
        conn.close()


def _require_owner(conn, run, worker_id: str = "") -> str:
    actual = str(run["owner_id"] or "")
    worker_id = str(worker_id or actual)
    if not worker_id or worker_id != actual:
        raise ValueError("当前对话框不是该任务的负责人；请使用 takeover 接管")
    conn.execute("UPDATE runs SET owner_heartbeat=?,updated_at=? WHERE run_id=?",
                 (utc_now(), utc_now(), run["run_id"]))
    return worker_id


def get_owner(workspace: str, run_id: str) -> Dict[str, Any]:
    conn = connect(workspace)
    try:
        run = require_run(conn, run_id)
        return {"run_id": run_id, "worker_id": run["owner_id"],
                "worker_label": run["owner_label"], "last_heartbeat": run["owner_heartbeat"]}
    finally:
        conn.close()


def _release_expired(conn, run_id: str) -> None:
    now = utc_now()
    rows = conn.execute(
        "SELECT job_id,node_id,lease_token FROM jobs WHERE run_id=? AND state='LEASED' AND lease_until<=?",
        (run_id, now),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE batch_jobs SET state='EXPIRED',updated_at=? WHERE job_id=? AND lease_token=? "
            "AND state='LEASED'", (now, row["job_id"], row["lease_token"]),
        )
        conn.execute(
            "UPDATE jobs SET state='PENDING',lease_until=NULL,lease_token='',eligible_at=?,updated_at=? WHERE job_id=?",
            (now, now, row["job_id"]),
        )
        event(conn, run_id, "JOB_LEASE_EXPIRED", row["node_id"], row["job_id"])
    conn.execute(
        "UPDATE batches SET state='EXPIRED',updated_at=? WHERE run_id=? AND state='LEASED' "
        "AND lease_until<=?", (now, run_id, now),
    )


def lease_jobs(workspace: str, run_id: str, limit: int = 20,
               worker_id: str = "") -> Dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    conn = connect(workspace)
    batch_id = ""
    try:
        with transaction(conn):
            run = require_run(conn, run_id)
            _release_expired(conn, run_id)
            now = utc_now()
            active = conn.execute(
                "SELECT * FROM batches WHERE run_id=? AND state='LEASED' AND lease_until>? "
                "ORDER BY created_at DESC LIMIT 1", (run_id, now),
            ).fetchone()
            if active:
                active_paths = _ipc_paths(workspace, run_id, active["batch_id"])
                return {"schema_version": PROTOCOL_VERSION, "run_id": run_id,
                        "batch_id": active["batch_id"], "jobs_count": int(active["job_count"]),
                        "jobs": [], "busy": True, "message": "已有活动批次，未重复领取任务",
                        "jobs_path": active_paths["jobs_path"],
                        "results_draft_path": active_paths["results_draft_path"],
                        "results_path": active_paths["results_path"],
                        "lease_until": active["lease_until"]}
            worker_id = str(worker_id or _new_id())
            rows = conn.execute(
                "SELECT * FROM jobs WHERE run_id=? AND state='PENDING' "
                "AND (eligible_at IS NULL OR eligible_at<=?) ORDER BY created_at,job_id LIMIT ?",
                (run_id, now, limit),
            ).fetchall()
            if not rows:
                return {"schema_version": PROTOCOL_VERSION, "run_id": run_id,
                        "batch_id": None, "jobs_count": 0, "jobs": [],
                        "jobs_path": None, "results_draft_path": None,
                        "results_path": None}
            batch_id = _new_id()
            paths = _ipc_paths(workspace, run_id, batch_id)
            lease_until = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO batches(batch_id,run_id,worker_id,state,relative_dir,job_count,lease_until,"
                "created_at,updated_at) VALUES(?,?,?,'LEASED',?,?,?,?,?)",
                (batch_id, run_id, worker_id, paths["relative_dir"], len(rows),
                 lease_until, now, now),
            )
            jobs = []
            for row in rows:
                token, attempt = _new_id(), int(row["attempts"]) + 1
                conn.execute(
                    "UPDATE jobs SET state='LEASED',attempts=?,lease_until=?,lease_token=?,updated_at=? "
                    "WHERE job_id=? AND state='PENDING'",
                    (attempt, lease_until, token, now, row["job_id"]),
                )
                args = {"amzSite": run["site"], "nodeid": row["node_id"]}
                if row["indicator"]:
                    args["indicator"] = row["indicator"]
                jobs.append({
                    "job_id": row["job_id"], "type": row["job_type"], "site": run["site"],
                    "node_id": row["node_id"], "indicator": row["indicator"] or None,
                    "arguments": args, "attempt": attempt, "lease_token": token,
                    "lease_until": lease_until, "batch_id": batch_id,
                })
                conn.execute(
                    "INSERT INTO batch_jobs VALUES(?,?,?,?, 'LEASED',?,?)",
                    (batch_id, row["job_id"], attempt, token, now, now),
                )
                event(conn, run_id, "JOB_LEASED", row["node_id"], {
                    "job_id": row["job_id"], "attempt": attempt, "batch_id": batch_id,
                })
            event(conn, run_id, "BATCH_LEASED", detail={
                "batch_id": batch_id, "worker_id": worker_id, "jobs": len(jobs),
            })
        document = {
            "schema_version": PROTOCOL_VERSION, "run_id": run_id,
            "batch_id": batch_id, "jobs_count": len(jobs),
            "jobs_path": paths["jobs_path"],
            "results_draft_path": paths["results_draft_path"],
            "results_path": paths["results_path"], "jobs": jobs,
        }
        try:
            _atomic_json(paths["jobs_path"], document)
        except OSError:
            with transaction(conn):
                snapshots = conn.execute(
                    "SELECT job_id,lease_token FROM batch_jobs WHERE batch_id=?", (batch_id,),
                ).fetchall()
                for snapshot in snapshots:
                    conn.execute(
                        "UPDATE jobs SET state='PENDING',lease_until=NULL,lease_token='',eligible_at=?,"
                        "updated_at=? WHERE job_id=? AND state='LEASED' AND lease_token=?",
                        (utc_now(), utc_now(), snapshot["job_id"], snapshot["lease_token"]),
                    )
                conn.execute("UPDATE batches SET state='ABANDONED',updated_at=? WHERE batch_id=?",
                             (utc_now(), batch_id))
            raise
        cleanup_ipc(workspace)
        return document
    finally:
        conn.close()


def _normalize_month(value: object) -> tuple[str, int]:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*月?", raw) or re.fullmatch(r"(\d{4})(\d{2})", raw)
    if not match:
        raise ValueError(f"无法识别月份格式: {raw!r}")
    year, month = map(int, match.groups())
    if not 1 <= month <= 12:
        raise ValueError(f"月份超出范围: {raw!r}")
    return f"{year:04d}-{month:02d}", year * 12 + month - 1


def _latest_complete_month_index() -> int:
    now = datetime.now(timezone.utc)
    return now.year * 12 + now.month - 2


def _normalize_trend_payload(payload: Any, indicator: str,
                             max_lag_months: int = 2) -> tuple[List[str], List[float]]:
    if not isinstance(payload, dict):
        raise ValueError("category_trend payload 必须是对象")
    months = payload.get("months")
    values = payload.get("values")
    if values is None:
        values = payload.get("sales" if indicator == "SalesCount" else "prices")
    if not isinstance(months, list) or not isinstance(values, list) or len(months) != len(values):
        raise ValueError("月份与数值数组缺失或长度不一致")
    if not 6 <= len(months) <= 24:
        raise ValueError(f"趋势数据必须为连续6至24个月: {len(months)}")
    parsed = [_normalize_month(month) for month in months]
    ordered = sorted(zip(parsed, values), key=lambda item: item[0][1])
    indexes = [item[0][1] for item in ordered]
    if len(set(indexes)) != len(indexes) or indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise ValueError("月份必须连续且不得重复")
    if _latest_complete_month_index() - indexes[-1] > int(max_lag_months):
        raise ValueError(f"数据过旧：最新月份 {ordered[-1][0][0]}，允许滞后 {max_lag_months} 个月")
    try:
        if any(isinstance(value, bool) for _, value in ordered):
            raise ValueError
        clean = [float(value) for _, value in ordered]
    except (TypeError, ValueError) as exc:
        raise ValueError("趋势数值必须为数字") from exc
    if any(not math.isfinite(value) for value in clean):
        raise ValueError("趋势数值必须是有限数字")
    return [item[0][0] for item in ordered], clean


def _normalize_tree_payload(payload: Any) -> List[Dict[str, str]]:
    raw = payload if isinstance(payload, list) else (payload.get("items") or payload.get("subcategories") or payload.get("data") or [] if isinstance(payload, dict) else None)
    if not isinstance(raw, list):
        raise ValueError("category_tree payload 必须是数组或包含 items 的对象")
    seen, items = set(), []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("子类目项必须是对象")
        node_id = _validate_node_id(item.get("id") or item.get("node_id") or item.get("nodeid"))
        name = str(item.get("name") or item.get("category_name") or "").strip()
        if not name:
            raise ValueError(f"子类目 {node_id} 缺少名称")
        if node_id not in seen:
            items.append({"id": node_id, "name": name}); seen.add(node_id)
    return items


def _write_decision(conn, run, node_id: str, result: Dict[str, Any],
                    label: str, reason: str, current_scale: float,
                    projected_scale: Optional[float] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO decisions(run_id,node_id,status,direction,sales_slope,price_slope,"
        "yoy_sales,yoy_price,avg_sales,avg_price,sales_r2,price_r2,current_scale,projected_scale,"
        "opportunity_level,reason,policy_version,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run["run_id"], node_id, result.get("status", "ok"), result.get("direction", ""),
         result.get("sales_slope_annual_pct"), result.get("price_slope_annual_pct"),
         float(result.get("yoy_sales_growth", 0)) * 100, float(result.get("yoy_price_growth", 0)) * 100,
         result.get("avg_monthly_sales"), result.get("avg_monthly_price"), result.get("sales_r2"),
         result.get("price_r2"), current_scale, projected_scale, label, reason,
         run["policy_version"], utc_now()),
    )
    conn.execute(
        "UPDATE nodes SET history_months=?,current_scale=?,updated_at=? WHERE run_id=? AND node_id=?",
        (int(result.get("history_months", 24)), current_scale, utc_now(), run["run_id"], node_id),
    )


def _evaluate_node(conn, run, node_id: str) -> None:
    node = conn.execute("SELECT * FROM nodes WHERE run_id=? AND node_id=?", (run["run_id"], node_id)).fetchone()
    policy = json.loads(run["policy_json"])
    series, months = {}, {}
    for indicator in ("SalesCount", "AvgPrice"):
        rows = conn.execute(
            "SELECT month,value FROM observations WHERE run_id=? AND node_id=? AND indicator=? ORDER BY month",
            (run["run_id"], node_id, indicator),
        ).fetchall()
        months[indicator] = [row["month"] for row in rows]
        series[indicator] = [float(row["value"]) for row in rows]
    if months["SalesCount"] != months["AvgPrice"]:
        raise ValueError("销量与均价月份不一致")
    count = len(series["SalesCount"])
    current_scale = sum(series["SalesCount"][-6:]) / 6
    threshold = float(policy["min_avg_sales"])
    if count < 24:
        synthetic = {
            "status": "short_history", "direction": "短历史",
            "avg_monthly_sales": sum(series["SalesCount"]) / count,
            "avg_monthly_price": sum(series["AvgPrice"]) / count,
            "history_months": count,
        }
        label = "SHORT_HISTORY_LOW_VOLUME" if current_scale < threshold else "SHORT_HISTORY_CANDIDATE"
        reason = f"仅有{count}个月连续新鲜数据；近6月均销{current_scale:.0f}"
        _write_decision(conn, run, node_id, synthetic, label, reason, current_scale)
        parent_was_short = conn.execute(
            "SELECT 1 FROM edges e JOIN nodes p ON p.run_id=e.run_id AND p.node_id=e.parent_id "
            "WHERE e.run_id=? AND e.child_id=? AND p.result='SHORT_HISTORY_EXPANDED'",
            (run["run_id"], node_id),
        ).fetchone()
        if parent_was_short:
            _state(conn, run["run_id"], node_id, "SHORT_HISTORY", "SHORT_HISTORY_CHILD_NO_REEXPAND",
                   reason + "；短历史父分支仅允许扩展一次，未继续下钻")
        elif current_scale < threshold:
            _state(conn, run["run_id"], node_id, "SHORT_HISTORY", label,
                   reason + "；体量不足，未下钻到底，子类目未获取")
            event(conn, run["run_id"], "BRANCH_PRUNED", node_id, {"reason": "short_history_low_volume"})
        elif int(node["level"]) >= int(policy["max_level"]):
            _state(conn, run["run_id"], node_id, "DEPTH_LIMIT", "SHORT_HISTORY_DEPTH_LIMIT", reason)
        else:
            _state(conn, run["run_id"], node_id, "WAITING_TREE", "SHORT_HISTORY_CANDIDATE", reason)
            _ensure_job(conn, run["run_id"], node_id, "category_tree")
        return
    result = judge_category_trend(
        series["SalesCount"], series["AvgPrice"], node_id=node_id, site=run["site"],
        min_avg_sales=threshold, sales_decline_tolerance=policy["sales_decline_tolerance"],
        price_decline_tolerance=policy["price_decline_tolerance"], min_r_squared=policy["min_r_squared"],
    )
    result["history_months"] = 24
    if result["status"] == "error":
        _state(conn, run["run_id"], node_id, "SUSPENDED", "DATA_ERROR", result["reason"]); return
    if current_scale < threshold:
        reg_sales = float(result.get("sales_slope_annual_pct") or 0) / 100
        yoy_sales = float(result.get("yoy_sales_growth") or 0)
        reg_price = float(result.get("price_slope_annual_pct") or 0) / 100
        yoy_price = float(result.get("yoy_price_growth") or 0)
        projected = current_scale * (1 + min(reg_sales, yoy_sales))
        candidate = (reg_sales > 0 and yoy_sales > 0 and float(result.get("sales_r2") or 0) >= float(policy["min_r_squared"])
                     and reg_price >= 0 and yoy_price >= 0 and projected >= threshold)
        label = "LOW_VOLUME_GROWTH_CANDIDATE" if candidate else "LOW_VOLUME_OBSERVATION"
        reason = ("低体量高增长观察：保守12月投影达到阈值" if candidate else "近6月体量不足，不构成正式机会")
        _write_decision(conn, run, node_id, result, label, reason, current_scale, projected)
        _state(conn, run["run_id"], node_id, "POLICY_PRUNED", label,
               reason + "；未下钻到底，子类目未获取")
        event(conn, run["run_id"], "BRANCH_PRUNED", node_id, {"reason": "low_volume", "current_scale": current_scale})
        return
    _write_decision(conn, run, node_id, result, "STANDARD", result.get("reason", ""), current_scale)
    if not result["is_valid_node"]:
        _state(conn, run["run_id"], node_id, "ELIMINATED", "ELIMINATED", result.get("reason", ""))
    elif int(node["level"]) >= int(policy["max_level"]):
        _state(conn, run["run_id"], node_id, "DEPTH_LIMIT", "DEPTH_LIMIT", "达到最大层级")
    else:
        _state(conn, run["run_id"], node_id, "WAITING_TREE", "TREND_PASSED", result.get("reason", ""))
        _ensure_job(conn, run["run_id"], node_id, "category_tree")


def _maybe_evaluate(conn, run, node_id: str) -> None:
    rows = conn.execute(
        "SELECT indicator,state FROM jobs WHERE run_id=? AND node_id=? AND job_type='category_trend'",
        (run["run_id"], node_id),
    ).fetchall()
    states = {row["indicator"]: row["state"] for row in rows}
    if all(states.get(indicator) == "SUCCEEDED" for indicator in INDICATORS):
        _state(conn, run["run_id"], node_id, "READY_TO_EVALUATE")
        _evaluate_node(conn, run, node_id)
    elif conn.execute(
        "SELECT state FROM nodes WHERE run_id=? AND node_id=?", (run["run_id"], node_id),
    ).fetchone()[0] not in TERMINAL_STATES:
        _state(conn, run["run_id"], node_id, "WAITING_TREND")


def _ingest_trend(conn, run, node, job, payload: Any) -> int:
    policy = json.loads(run["policy_json"])
    months, values = _normalize_trend_payload(payload, job["indicator"], policy["max_data_lag_months"])
    other = "AvgPrice" if job["indicator"] == "SalesCount" else "SalesCount"
    other_months = [row[0] for row in conn.execute(
        "SELECT month FROM observations WHERE run_id=? AND node_id=? AND indicator=? ORDER BY month",
        (run["run_id"], node["node_id"], other),
    ).fetchall()]
    if other_months and other_months != months:
        raise ValueError("销量与均价月份不一致")
    now = utc_now()
    conn.execute("DELETE FROM observations WHERE run_id=? AND node_id=? AND indicator=?",
                 (run["run_id"], node["node_id"], job["indicator"]))
    conn.executemany(
        "INSERT INTO observations VALUES(?,?,?,?,?,?)",
        [(run["run_id"], node["node_id"], job["indicator"], month, value, now)
         for month, value in zip(months, values)],
    )
    return len(values)


def _ingest_tree(conn, run, node, payload: Any) -> int:
    items = _normalize_tree_payload(payload)
    if any(item["id"] == node["node_id"] for item in items):
        raise ValueError("类目树不能包含自身")
    now = utc_now()
    is_root = str(node["node_id"]) == str(run["root_node_id"])
    short_parent = node["result"] == "SHORT_HISTORY_CANDIDATE"
    if items:
        _state(conn, run["run_id"], node["node_id"], "SHORT_HISTORY" if short_parent else "EXPANDED",
               "SHORT_HISTORY_EXPANDED" if short_parent else "EXPANDED")
        conn.execute("UPDATE nodes SET descendants_known=1 WHERE run_id=? AND node_id=?", (run["run_id"], node["node_id"]))
    else:
        if is_root:
            _state(conn, run["run_id"], node["node_id"], "EXPANDED", "ROOT_EMPTY")
            conn.execute("UPDATE nodes SET descendants_known=1 WHERE run_id=? AND node_id=?",
                         (run["run_id"], node["node_id"]))
        else:
            _state(conn, run["run_id"], node["node_id"], "SHORT_HISTORY" if short_parent else "OPPORTUNITY",
                   "SHORT_HISTORY_LEAF" if short_parent else "LEAF_OPPORTUNITY")
    for item in items:
        child_path = f"{node['path']}/{item['name']}"
        conn.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                     (run["run_id"], node["node_id"], item["id"], item["name"], now))
        if not conn.execute("SELECT 1 FROM nodes WHERE run_id=? AND node_id=?", (run["run_id"], item["id"])).fetchone():
            conn.execute(
                "INSERT INTO nodes(run_id,node_id,path,level,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (run["run_id"], item["id"], child_path, int(node["level"]) + 1, "PENDING_TREND", now, now),
            )
            _ensure_trend_jobs(conn, run["run_id"], item["id"])
            event(conn, run["run_id"], "NODE_DISCOVERED", item["id"], {"parent_id": node["node_id"], "path": child_path})
    return len(items)


def _record_failure(conn, run, job, error: str, defer_terminal: bool = False) -> bool:
    attempts, maximum = int(job["attempts"]), int(json.loads(run["policy_json"])["max_attempts"])
    terminal = attempts >= maximum
    delay = 30 if attempts == 1 else 120
    eligible = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE jobs SET state=?,lease_until=NULL,lease_token='',eligible_at=?,last_error=?,updated_at=? WHERE job_id=?",
        ("FAILED" if terminal else "PENDING", None if terminal else eligible, error, utc_now(), job["job_id"]),
    )
    if terminal and not defer_terminal:
        _state(conn, run["run_id"], job["node_id"], "SUSPENDED", "MCP_OR_DATA_FAILED", error)
        conn.execute(
            "UPDATE jobs SET state='CANCELED',lease_until=NULL,lease_token='',last_error='NODE_SUSPENDED',updated_at=? "
            "WHERE run_id=? AND node_id=? AND job_id<>? AND state IN ('PENDING','LEASED')",
            (utc_now(), run["run_id"], job["node_id"], job["job_id"]),
        )
    else:
        if not terminal:
            event(conn, run["run_id"], "JOB_RETRY_SCHEDULED", job["node_id"], {
                "job_id": job["job_id"], "attempt": attempts, "eligible_at": eligible, "error": error,
            })
    return terminal


def _depth_events(conn, run_id: str) -> None:
    levels = [row[0] for row in conn.execute("SELECT DISTINCT level FROM nodes WHERE run_id=? ORDER BY level", (run_id,))]
    for level in levels:
        active = conn.execute(
            "SELECT COUNT(*) FROM nodes n WHERE n.run_id=? AND n.level=? AND (n.state NOT IN (?,?,?,?,?,?,?) "
            "OR EXISTS(SELECT 1 FROM jobs j WHERE j.run_id=n.run_id AND j.node_id=n.node_id AND j.state IN ('PENDING','LEASED')))",
            (run_id, level, *sorted(TERMINAL_STATES)),
        ).fetchone()[0]
        if not active:
            event(conn, run_id, "DEPTH_COMPLETED", str(level), "complete")


def _run_status(conn, run_id: str) -> str:
    active_nodes = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE run_id=? AND state IN (?,?,?,?)", (run_id, *sorted(ACTIVE_STATES)),
    ).fetchone()[0]
    active_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND state IN ('PENDING','LEASED')", (run_id,)).fetchone()[0]
    suspended = conn.execute("SELECT COUNT(*) FROM nodes WHERE run_id=? AND state='SUSPENDED'", (run_id,)).fetchone()[0]
    state = "RUNNING" if active_nodes or active_jobs else ("PARTIAL_DONE" if suspended else "ALL_DONE")
    now = utc_now()
    current = conn.execute("SELECT status,completed_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if current and (current["status"] != state or (state == "ALL_DONE" and not current["completed_at"])):
        conn.execute(
            "UPDATE runs SET status=?,updated_at=?,completed_at=CASE WHEN ?='ALL_DONE' THEN COALESCE(completed_at,?) ELSE completed_at END WHERE run_id=?",
            (state, now, state, now, run_id),
        )
    _depth_events(conn, run_id)
    return state


def ingest_results(workspace: str, run_id: str, document: Dict[str, Any],
                   batch_id: str = "", worker_id: str = "") -> Dict[str, Any]:
    protocol = str(document.get("schema_version") or "")
    if protocol not in {PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION} or document.get("run_id") != run_id:
        raise ValueError("results 协议版本或 run_id 不匹配")
    results = document.get("results")
    if not isinstance(results, list):
        raise ValueError("results 必须是数组")
    batch_id = str(batch_id or document.get("batch_id") or "")
    if protocol == PROTOCOL_VERSION and not batch_id:
        raise ValueError("协议 v3 回写必须提供 batch_id")
    conn = connect(workspace)
    accepted = duplicate = failed = 0
    deferred_suspensions: Dict[str, str] = {}
    try:
        with transaction(conn):
            run = require_run(conn, run_id)
            snapshots: Dict[str, Any] = {}
            if protocol == PROTOCOL_VERSION:
                batch = conn.execute(
                    "SELECT * FROM batches WHERE batch_id=? AND run_id=?", (batch_id, run_id),
                ).fetchone()
                if not batch:
                    raise ValueError("batch_id 不存在或不属于指定 Run")
                if batch["state"] == "INGESTED":
                    if batch["receipt_json"]:
                        return json.loads(batch["receipt_json"])
                    raise ValueError("批次已经完成，但回执缺失")
                if batch["state"] != "LEASED":
                    raise ValueError(f"批次已经失效: {batch['state']}")
                if batch["lease_until"] and batch["lease_until"] <= utc_now():
                    _release_expired(conn, run_id)
                    raise ValueError("批次租约已经过期，旧结果已拒绝")
                snapshots = {row["job_id"]: row for row in conn.execute(
                    "SELECT * FROM batch_jobs WHERE batch_id=?", (batch_id,),
                )}
                supplied = [str(item.get("job_id") or "") for item in results if isinstance(item, dict)]
                if len(supplied) != len(results):
                    raise ValueError("每个 result 必须是对象")
                if len(supplied) != len(set(supplied)):
                    raise ValueError("批次结果包含重复 job_id")
                missing = sorted(set(snapshots) - set(supplied))
                extra = sorted(set(supplied) - set(snapshots))
                if missing or extra or len(supplied) != int(batch["job_count"]):
                    raise ValueError(f"批次结果不完整或越界；缺少={missing}，多余={extra}")
            for result in results:
                if not isinstance(result, dict):
                    raise ValueError("每个 result 必须是对象")
                receipt_hash, job_id = _canonical_hash(result), str(result.get("job_id") or "")
                job = conn.execute("SELECT * FROM jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
                if not job:
                    raise ValueError(f"任务不存在或不属于本运行: {job_id}")
                if protocol == PROTOCOL_VERSION:
                    snapshot = snapshots[job_id]
                    if (int(result.get("attempt", 0)) != int(snapshot["attempt"]) or
                            result.get("lease_token") != snapshot["lease_token"]):
                        raise ValueError(f"任务 {job_id} 的租约令牌与批次快照不匹配")
                elif conn.execute(
                    "SELECT 1 FROM batch_jobs WHERE job_id=? AND lease_token=?",
                    (job_id, job["lease_token"]),
                ).fetchone():
                    raise ValueError("协议 v3 领取的任务不能按 v2 回写")
                if conn.execute("SELECT 1 FROM ingest_receipts WHERE job_id=? AND receipt_hash=?", (job_id, receipt_hash)).fetchone():
                    duplicate += 1; continue
                if job["state"] != "LEASED" or result.get("lease_token") != job["lease_token"] or int(result.get("attempt", 0)) != int(job["attempts"]):
                    raise ValueError(f"任务 {job_id} 的租约已失效或令牌不匹配")
                if job["lease_until"] and job["lease_until"] <= utc_now():
                    raise ValueError(f"任务 {job_id} 的租约已过期")
                conn.execute("INSERT INTO ingest_receipts VALUES(?,?,?)", (job_id, receipt_hash, utc_now()))
                call_type = job["job_type"] + ((":" + job["indicator"]) if job["indicator"] else "")
                result_status, error, data_count = result.get("status"), str(result.get("error") or ""), 0
                if result_status not in {"success", "failed"}:
                    raise ValueError("result.status 只能是 success 或 failed")
                node = conn.execute("SELECT * FROM nodes WHERE run_id=? AND node_id=?", (run_id, job["node_id"])).fetchone()
                if result_status == "success":
                    try:
                        data_count = _ingest_tree(conn, run, node, result.get("payload")) if job["job_type"] == "category_tree" else _ingest_trend(conn, run, node, job, result.get("payload"))
                    except ValueError as exc:
                        result_status, error = "failed", str(exc)
                if result_status == "failed":
                    error = error or "MCP调用失败"
                    terminal = _record_failure(conn, run, job, error, protocol == PROTOCOL_VERSION)
                    if terminal and protocol == PROTOCOL_VERSION:
                        deferred_suspensions[job["node_id"]] = error
                    failed += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET state='SUCCEEDED',lease_until=NULL,lease_token='',last_error='',result_hash=?,updated_at=? WHERE job_id=?",
                        (_canonical_hash(result.get("payload")), utc_now(), job_id),
                    )
                    if job["job_type"] == "category_trend":
                        _maybe_evaluate(conn, run, job["node_id"])
                    accepted += 1
                conn.execute(
                    "INSERT INTO mcp_attempts(run_id,job_id,node_id,site,call_type,status,error,data_count,attempt_no,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, job_id, job["node_id"], run["site"], call_type,
                     "成功" if result_status == "success" else "失败", error, data_count, job["attempts"], utc_now()),
                )
                if protocol == PROTOCOL_VERSION:
                    conn.execute(
                        "UPDATE batch_jobs SET state=?,updated_at=? WHERE batch_id=? AND job_id=?",
                        ("SUCCEEDED" if result_status == "success" else "FAILED", utc_now(), batch_id, job_id),
                    )
            for node_id, error in deferred_suspensions.items():
                _state(conn, run_id, node_id, "SUSPENDED", "MCP_OR_DATA_FAILED", error)
                conn.execute(
                    "UPDATE jobs SET state='CANCELED',lease_until=NULL,lease_token='',"
                    "last_error='NODE_SUSPENDED',updated_at=? WHERE run_id=? AND node_id=? "
                    "AND state IN ('PENDING','LEASED')",
                    (utc_now(), run_id, node_id),
                )
            run_status = _run_status(conn, run_id)
            receipt = {"schema_version": PROTOCOL_VERSION, "run_id": run_id,
                       "batch_id": batch_id or None, "accepted": accepted,
                       "duplicates": duplicate, "failed": failed, "run_status": run_status,
                       "legacy_protocol": protocol == LEGACY_PROTOCOL_VERSION}
            if protocol == PROTOCOL_VERSION:
                receipt_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
                conn.execute(
                    "UPDATE batches SET state='INGESTED',completed_jobs=?,failed_jobs=?,stage='INGESTED',"
                    "heartbeat_at=?,receipt_hash=?,receipt_json=?,updated_at=?,ingested_at=? WHERE batch_id=?",
                    (accepted + duplicate, failed, utc_now(), _canonical_hash(receipt), receipt_json,
                     utc_now(), utc_now(), batch_id),
                )
            event(conn, run_id, "BATCH_INGESTED", detail={
                "batch_id": batch_id or None, "accepted": accepted, "duplicate": duplicate,
                "failed": failed, "status": run_status, "protocol": protocol,
            })
        return receipt
    finally:
        conn.close()


def ingest_batch(workspace: str, run_id: str, batch_id: str,
    worker_id: str = "") -> Dict[str, Any]:
    paths = _ipc_paths(workspace, run_id, batch_id)
    conn = connect(workspace)
    try:
        batch = conn.execute(
            "SELECT state,receipt_json FROM batches WHERE batch_id=? AND run_id=?",
            (batch_id, run_id),
        ).fetchone()
        if not batch:
            raise ValueError("batch_id 不存在或不属于指定 Run")
        if batch["state"] == "INGESTED" and batch["receipt_json"]:
            receipt = json.loads(batch["receipt_json"])
            _atomic_json(paths["receipt_path"], receipt)
            return receipt
    finally:
        conn.close()
    source = paths["results_draft_path"] if os.path.isfile(paths["results_draft_path"]) else paths["results_path"]
    if not os.path.isfile(source):
        raise ValueError(f"找不到批次结果草稿: {paths['results_draft_path']}")
    with open(source, "r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if source == paths["results_draft_path"]:
        os.replace(paths["results_draft_path"], paths["results_path"])
    receipt = ingest_results(workspace, run_id, document, batch_id=batch_id, worker_id=worker_id)
    _atomic_json(paths["receipt_path"], receipt)
    return receipt


def pulse_batch(workspace: str, run_id: str, batch_id: str, *, completed: int = 0,
                failed: int = 0, current_node_id: str = "", stage: str = "CALLING_MCP") -> Dict[str, Any]:
    """Publish non-authoritative batch progress for panel/chat visibility."""
    completed, failed = max(0, int(completed)), max(0, int(failed))
    stage = str(stage or "CALLING_MCP").strip().upper()[:40]
    current_node_id = str(current_node_id or "").strip()[:64]
    conn = connect(workspace)
    try:
        with transaction(conn):
            batch = conn.execute("SELECT * FROM batches WHERE batch_id=? AND run_id=?", (batch_id, run_id)).fetchone()
            if not batch:
                raise ValueError("batch_id 不存在或不属于指定 Run")
            if batch["state"] != "LEASED":
                raise ValueError(f"批次已经失效: {batch['state']}")
            completed = max(completed, int(batch["completed_jobs"] or 0))
            failed = max(failed, int(batch["failed_jobs"] or 0))
            if completed + failed > int(batch["job_count"]):
                raise ValueError("批次进度超过任务总数")
            now = utc_now()
            conn.execute(
                "UPDATE batches SET completed_jobs=?,failed_jobs=?,heartbeat_at=?,current_node_id=?,stage=?,updated_at=? WHERE batch_id=?",
                (completed, failed, now, current_node_id, stage, now, batch_id),
            )
        return {"run_id": run_id, "batch_id": batch_id, "completed": completed,
                "failed": failed, "total": int(batch["job_count"]), "stage": stage,
                "heartbeat_at": now}
    finally:
        conn.close()


def list_batches(workspace: str, run_id: str) -> List[Dict[str, Any]]:
    conn = connect(workspace)
    try:
        require_run(conn, run_id)
        return [dict(row) for row in conn.execute(
            "SELECT batch_id,run_id,worker_id,state,relative_dir,job_count,lease_until,"
            "completed_jobs,failed_jobs,heartbeat_at,current_node_id,stage,"
            "created_at,updated_at,ingested_at FROM batches WHERE run_id=? "
            "ORDER BY created_at DESC", (run_id,),
        )]
    finally:
        conn.close()


def takeover_run(workspace: str, run_id: str, force: bool = False,
                 worker_label: str = "WorkBuddy/接管") -> Dict[str, Any]:
    conn = connect(workspace)
    try:
        with transaction(conn):
            require_run(conn, run_id)
            _release_expired(conn, run_id)
            now = utc_now()
            active = conn.execute(
                "SELECT * FROM batches WHERE run_id=? AND state='LEASED' AND lease_until>?",
                (run_id, now),
            ).fetchall()
            if active and not force:
                latest = max(row["lease_until"] for row in active)
                raise ValueError(f"仍有有效批次租约，最晚到期时间: {latest}；如确认中止旧批次，请显式使用 --force")
            canceled = 0
            if force:
                for batch in active:
                    snapshots = conn.execute(
                        "SELECT * FROM batch_jobs WHERE batch_id=? AND state='LEASED'",
                        (batch["batch_id"],),
                    ).fetchall()
                    for snapshot in snapshots:
                        conn.execute(
                            "UPDATE jobs SET state='PENDING',lease_until=NULL,lease_token='',"
                            "eligible_at=?,updated_at=? WHERE job_id=? AND state='LEASED' AND lease_token=?",
                            (now, now, snapshot["job_id"], snapshot["lease_token"]),
                        )
                    conn.execute(
                        "UPDATE batch_jobs SET state='CANCELED',updated_at=? "
                        "WHERE batch_id=? AND state='LEASED'", (now, batch["batch_id"]),
                    )
                    conn.execute(
                        "UPDATE batches SET state='ABANDONED',updated_at=? WHERE batch_id=?",
                        (now, batch["batch_id"]),
                    )
                    canceled += 1
            worker_id = _new_id()
            conn.execute(
                "UPDATE runs SET owner_id=?,owner_label=?,owner_heartbeat=?,updated_at=? WHERE run_id=?",
                (worker_id, worker_label.strip() or "WorkBuddy/接管", now, now, run_id),
            )
            event(conn, run_id, "RUN_TAKEN_OVER", detail={
                "worker_id": worker_id, "worker_label": worker_label,
                "forced": bool(force), "canceled_batches": canceled,
            })
        return {"run_id": run_id, "worker_id": worker_id,
                "worker_label": worker_label, "forced": bool(force),
                "canceled_batches": canceled}
    finally:
        conn.close()


def resume_run(workspace: str, run_id: str, all_suspended: bool = False) -> Dict[str, Any]:
    if not all_suspended:
        raise ValueError("恢复暂停节点需要显式传入 --all-suspended")
    conn = connect(workspace)
    try:
        with transaction(conn):
            require_run(conn, run_id)
            rows = conn.execute("SELECT node_id FROM nodes WHERE run_id=? AND state='SUSPENDED'", (run_id,)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET state='PENDING',attempts=0,lease_until=NULL,lease_token='',eligible_at=?,last_error='',updated_at=? "
                    "WHERE run_id=? AND node_id=? AND state IN ('FAILED','CANCELED')",
                    (utc_now(), utc_now(), run_id, row["node_id"]),
                )
                pending_tree = conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND node_id=? AND job_type='category_tree' AND state='PENDING'", (run_id, row["node_id"])).fetchone()
                _state(conn, run_id, row["node_id"], "WAITING_TREE" if pending_tree else "WAITING_TREND")
            current = _run_status(conn, run_id)
        return {"run_id": run_id, "resumed_nodes": len(rows), "status": current}
    finally:
        conn.close()


def status(workspace: str, run_id: str) -> Dict[str, Any]:
    conn = connect(workspace)
    try:
        with transaction(conn):
            require_run(conn, run_id); _release_expired(conn, run_id); _run_status(conn, run_id)
        run = require_run(conn, run_id)
        node_counts = {row["state"]: int(row["cnt"]) for row in conn.execute("SELECT state,COUNT(*) cnt FROM nodes WHERE run_id=? GROUP BY state", (run_id,))}
        job_counts = {row["state"]: int(row["cnt"]) for row in conn.execute("SELECT state,COUNT(*) cnt FROM jobs WHERE run_id=? GROUP BY state", (run_id,))}
        attempts = int(conn.execute("SELECT COUNT(*) FROM mcp_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        opportunities = int(conn.execute("SELECT COUNT(*) FROM nodes WHERE run_id=? AND state='OPPORTUNITY'", (run_id,)).fetchone()[0])
        active_batch = conn.execute(
            "SELECT batch_id,lease_until,worker_id,job_count,completed_jobs,failed_jobs,"
            "heartbeat_at,current_node_id,stage FROM batches WHERE run_id=? AND state='LEASED' "
            "ORDER BY created_at DESC LIMIT 1", (run_id,),
        ).fetchone()
        active_leases = int(conn.execute(
            "SELECT COUNT(*) FROM batches WHERE run_id=? AND state='LEASED' AND lease_until>?",
            (run_id, utc_now()),
        ).fetchone()[0])
        waiting_retry = int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id=? AND state='PENDING' AND eligible_at>?",
            (run_id, utc_now()),
        ).fetchone()[0])
        total_nodes = sum(node_counts.values())
        terminal_nodes = sum(node_counts.get(state, 0) for state in TERMINAL_STATES)
        current_level = int(conn.execute(
            "SELECT COALESCE(MAX(level),0) FROM nodes WHERE run_id=?", (run_id,),
        ).fetchone()[0])
        task = conn.execute(
            "SELECT request_code FROM task_requests WHERE run_id=? LIMIT 1", (run_id,),
        ).fetchone()
        from .insights import scan_insights
        insight = scan_insights(conn, run_id)
        activity_status = run["status"]
        if run["status"] == "RUNNING":
            if active_batch:
                activity_status = active_batch["stage"] or "BACKGROUND_RUNNING"
            elif waiting_retry:
                activity_status = "WAITING_RETRY"
            elif job_counts.get("PENDING", 0):
                trend_pending = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE run_id=? AND state='PENDING' AND job_type='category_trend'", (run_id,),
                ).fetchone()[0]
                activity_status = "FETCHING_TREND" if trend_pending else "FETCHING_TREE"
            else:
                activity_status = "EVALUATING"
        batch_progress = None
        if active_batch:
            done = int(active_batch["completed_jobs"] or 0) + int(active_batch["failed_jobs"] or 0)
            batch_progress = {
                "batch_id": active_batch["batch_id"], "total": int(active_batch["job_count"]),
                "completed": int(active_batch["completed_jobs"] or 0),
                "failed": int(active_batch["failed_jobs"] or 0), "processed": done,
                "percent": round(done / int(active_batch["job_count"]) * 100.0, 1) if active_batch["job_count"] else 0.0,
                "stage": active_batch["stage"], "current_node_id": active_batch["current_node_id"],
                "heartbeat_at": active_batch["heartbeat_at"], "lease_until": active_batch["lease_until"],
            }
        return {
            "schema_version": PROTOCOL_VERSION, "run_id": run_id, "site": run["site"],
            "root_node_id": run["root_node_id"], "category": run["category"],
            "policy_version": run["policy_version"], "policy": json.loads(run["policy_json"]),
            "status": run["status"], "activity_status": activity_status,
            "node_counts": node_counts, "job_counts": job_counts,
            "mcp_attempts": attempts, "opportunities": opportunities,
            "completion_ok": run["status"] == "ALL_DONE", "created_at": run["created_at"],
            "updated_at": run["updated_at"], "completed_at": run["completed_at"],
            "operator_name": run["operator_name"], "insights": insight,
            "task_code": task["request_code"] if task else None,
            "worker_id": run["owner_id"], "worker_label": run["owner_label"],
            "last_heartbeat": run["owner_heartbeat"],
            "active_batch_id": active_batch["batch_id"] if active_batch else None,
            "active_batch_lease_until": active_batch["lease_until"] if active_batch else None,
            "active_leases": active_leases, "waiting_retry": waiting_retry,
            "current_level": current_level, "total_nodes": total_nodes,
            "terminal_nodes": terminal_nodes,
            "batch_progress": batch_progress,
            "progress_basis": "当前已发现节点闭环率（扫描中分母会随新节点增加）",
            "completion_percent": round((terminal_nodes / total_nodes * 100.0) if total_nodes else 0.0, 1),
        }
    finally:
        conn.close()


def list_runs(workspace: str) -> List[Dict[str, Any]]:
    conn = connect(workspace)
    try:
        rows = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return [status(workspace, row["run_id"]) for row in rows]
