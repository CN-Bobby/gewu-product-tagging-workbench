"""Local identity and immutable task-request ledger."""
from __future__ import annotations

import json
import random
import string
import uuid

from .db import connect, default_report_dir, transaction, utc_now
from .engine import _create_run_in_conn, _validate_node_id, _validated_policy, status
from .site_profiles import normalize_site


def get_profile(workspace: str) -> dict:
    conn = connect(workspace)
    try:
        row = conn.execute("SELECT * FROM local_profile WHERE profile_id=1").fetchone()
        if not row:
            with transaction(conn):
                conn.execute("INSERT INTO local_profile VALUES(1,?,?,?,?,?)", (
                    str(uuid.uuid4()), "本机操作人", str(uuid.uuid4()), "Windows设备", utc_now(),
                ))
            row = conn.execute("SELECT * FROM local_profile WHERE profile_id=1").fetchone()
        return dict(row)
    finally:
        conn.close()


def save_profile(workspace: str, display_name: str, device_name: str) -> dict:
    current = get_profile(workspace)
    display_name, device_name = display_name.strip(), device_name.strip()
    if not display_name or not device_name:
        raise ValueError("操作人和设备名不能为空")
    conn = connect(workspace)
    try:
        with transaction(conn):
            conn.execute("UPDATE local_profile SET display_name=?,device_name=?,updated_at=? WHERE profile_id=1",
                         (display_name, device_name, utc_now()))
        current.update({"display_name": display_name, "device_name": device_name})
        return current
    finally:
        conn.close()


def _code() -> str:
    return "XLJ-" + "".join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(4))


def create_request(workspace: str, site: str, root_node_id: str, category: str,
                   policy: dict | None = None, report_dir: str = "", cloned_from: str = "") -> dict:
    site = normalize_site(site)
    root_node_id = _validate_node_id(root_node_id)
    category = str(category or "").strip()
    if not category:
        raise ValueError("类目名不能为空")
    chosen = _validated_policy(policy)
    profile, code = get_profile(workspace), _code()
    conn = connect(workspace)
    try:
        with transaction(conn):
            while conn.execute("SELECT 1 FROM task_requests WHERE request_code=?", (code,)).fetchone():
                code = _code()
            conn.execute(
                "INSERT INTO task_requests(request_code,request_id,status,site,root_node_id,category,policy_json,"
                "report_dir,created_by,created_at,cloned_from) VALUES(?,?,'DRAFT',?,?,?,?,?,?,?,?)",
                (code, str(uuid.uuid4()), site, root_node_id, category,
                 json.dumps(chosen, ensure_ascii=False, sort_keys=True), report_dir or default_report_dir(),
                 profile["display_name"], utc_now(), cloned_from),
            )
        return get_request(workspace, code)
    finally:
        conn.close()


def get_request(workspace: str, code: str) -> dict:
    conn = connect(workspace)
    try:
        row = conn.execute("SELECT * FROM task_requests WHERE request_code=?", (code.upper(),)).fetchone()
        if not row: raise ValueError(f"任务码不存在: {code}")
        value = dict(row); value["policy"] = json.loads(value.pop("policy_json")); return value
    finally:
        conn.close()


def delete_draft_request(workspace: str, code: str) -> dict:
    code = code.upper()
    conn = connect(workspace)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT status FROM task_requests WHERE request_code=?", (code,),
            ).fetchone()
            if not row:
                raise ValueError(f"任务码不存在: {code}")
            if row["status"] != "DRAFT":
                raise ValueError("只能删除尚未领取的 DRAFT 草稿")
            conn.execute("DELETE FROM task_requests WHERE request_code=?", (code,))
        return {"request_code": code, "deleted": True}
    finally:
        conn.close()


def claim_request(workspace: str, code: str) -> dict:
    code = code.upper()
    request = get_request(workspace, code)
    if request["status"] == "CLAIMED":
        return {"request_code": code, "run_id": request["run_id"], "already_claimed": True,
                "start_allowed": False, "message": "任务已由其他对话框领取",
                "current_status": status(workspace, request["run_id"])}
    conn = connect(workspace)
    try:
        existing_run = ""
        owner_id = ""
        with transaction(conn):
            locked = conn.execute("SELECT * FROM task_requests WHERE request_code=?", (code,)).fetchone()
            if locked and locked["status"] == "CLAIMED" and locked["run_id"]:
                existing_run = locked["run_id"]
            elif not locked or locked["status"] != "DRAFT":
                raise RuntimeError("任务正在被另一个对话框领取或已经领取")
            else:
                run_id = _create_run_in_conn(
                    conn, request["site"], request["root_node_id"], request["category"],
                    policy=request["policy"], operator_name=request["created_by"],
                    report_dir=request["report_dir"],
                )
                conn.execute(
                    "UPDATE task_requests SET status='CLAIMED',claimed_at=?,run_id=? WHERE request_code=?",
                    (utc_now(), run_id, code),
                )
                conn.execute(
                    "UPDATE runs SET owner_label=? WHERE run_id=?",
                    (f"WorkBuddy/{code}/{request['created_by']}", run_id),
                )
                owner_id = conn.execute("SELECT owner_id FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        if existing_run:
            return {"request_code": code, "run_id": existing_run, "already_claimed": True,
                    "start_allowed": False, "message": "任务已由其他对话框领取",
                    "current_status": status(workspace, existing_run)}
        return {"request_code": code, "run_id": run_id,
                "worker_id": owner_id, "already_claimed": False,
                "start_allowed": True}
    finally:
        conn.close()


def list_requests(workspace: str) -> list[dict]:
    conn = connect(workspace)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM task_requests ORDER BY created_at DESC")]
    finally:
        conn.close()


def save_preset(workspace: str, name: str, site: str, root_node_id: str = "",
                category: str = "", policy: dict | None = None, report_dir: str = "",
                description: str = "", is_default: bool = False) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("预设名称不能为空")
    site = normalize_site(site)
    root_node_id = str(root_node_id or "").strip()
    if root_node_id:
        root_node_id = _validate_node_id(root_node_id)
    chosen, now = _validated_policy(policy), utc_now()
    conn = connect(workspace)
    try:
        with transaction(conn):
            existing = conn.execute("SELECT preset_id,created_at FROM parameter_presets WHERE name=?", (name,)).fetchone()
            preset_id = existing["preset_id"] if existing else str(uuid.uuid4())
            created_at = existing["created_at"] if existing else now
            if is_default:
                conn.execute("UPDATE parameter_presets SET is_default=0")
            conn.execute(
                "INSERT OR REPLACE INTO parameter_presets(preset_id,name,description,site,root_node_id,category,"
                "policy_json,report_dir,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (preset_id, name, str(description or "").strip(), site, root_node_id,
                 str(category or "").strip(), json.dumps(chosen, ensure_ascii=False, sort_keys=True),
                 str(report_dir or "").strip(), 1 if is_default else 0, created_at, now),
            )
        return get_preset(workspace, preset_id)
    finally:
        conn.close()


def get_preset(workspace: str, preset_id: str) -> dict:
    conn = connect(workspace)
    try:
        row = conn.execute("SELECT * FROM parameter_presets WHERE preset_id=?", (preset_id,)).fetchone()
        if not row:
            raise ValueError(f"参数预设不存在: {preset_id}")
        value = dict(row)
        value["policy"] = json.loads(value.pop("policy_json"))
        return value
    finally:
        conn.close()


def list_presets(workspace: str) -> list[dict]:
    conn = connect(workspace)
    try:
        values = []
        for row in conn.execute("SELECT * FROM parameter_presets ORDER BY is_default DESC,updated_at DESC,name"):
            value = dict(row); value["policy"] = json.loads(value.pop("policy_json")); values.append(value)
        return values
    finally:
        conn.close()


def delete_preset(workspace: str, preset_id: str) -> dict:
    conn = connect(workspace)
    try:
        with transaction(conn):
            row = conn.execute("SELECT name FROM parameter_presets WHERE preset_id=?", (preset_id,)).fetchone()
            if not row:
                raise ValueError(f"参数预设不存在: {preset_id}")
            conn.execute("DELETE FROM parameter_presets WHERE preset_id=?", (preset_id,))
        return {"preset_id": preset_id, "name": row["name"], "deleted": True}
    finally:
        conn.close()


def launch_instruction(site: str, root_node_id: str, category: str, policy: dict,
                       operator_name: str = "", report_dir: str = "") -> str:
    chosen = _validated_policy(policy)
    site, root_node_id = normalize_site(site), _validate_node_id(root_node_id)
    category = str(category or "").strip()
    if not category:
        raise ValueError("类目名不能为空")
    parts = [
        f"请执行寻龙诀：站点 {site}，根 Node ID {root_node_id}，类目 {category}。",
        f"操作人：{operator_name.strip() or '使用本机身份'}。",
        "参数：" + "，".join([
            f"近6月均销门槛 {chosen['min_avg_sales']:g}", f"最大层级 {chosen['max_level']}",
            f"销量下降容忍 {chosen['sales_decline_tolerance']:g}",
            f"价格下降容忍 {chosen['price_decline_tolerance']:g}",
            f"最低R² {chosen['min_r_squared']:g}", f"批量 {chosen['batch_size']}",
            f"重试 {chosen['max_attempts']} 次", f"数据滞后不超过 {chosen['max_data_lag_months']} 月",
        ]) + "。",
    ]
    if report_dir:
        parts.append(f"报告目录：{report_dir}。")
    launch_payload = {
        "schema_version": "xlj-launch/1",
        "site": site,
        "root_node": root_node_id,
        "category": category,
        "operator": operator_name.strip(),
        "report_dir": report_dir,
        "cli_policy": {
            "volume_threshold": chosen["min_avg_sales"],
            "max_level": chosen["max_level"],
            "sales_decline_tolerance": chosen["sales_decline_tolerance"],
            "price_decline_tolerance": chosen["price_decline_tolerance"],
            "min_r_squared": chosen["min_r_squared"],
            "batch_size": chosen["batch_size"],
            "max_attempts": chosen["max_attempts"],
            "max_data_lag_months": chosen["max_data_lag_months"],
        },
    }
    parts.append("结构化启动参数（必须原样解析，不要猜测参数名）：\n```json\n" +
                 json.dumps(launch_payload, ensure_ascii=False, separators=(",", ":")) + "\n```")
    parts.append("请始终使用已安装的稳定启动器 xlj.ps1；创建或复用同参数的活动 Run，自动闭环到 ALL_DONE 或 PARTIAL_DONE。每批和每层汇报进度，不因工作量大停工请示；报告只能使用 CLI 返回的成品路径。")
    return "\n".join(parts)
