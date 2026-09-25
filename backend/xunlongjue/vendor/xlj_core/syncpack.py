"""Manual, immutable cross-device .xljpack export/import."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile

from .db import connect, transaction, utc_now
from .ledger import get_profile

TABLES = ("runs", "nodes", "edges", "observations", "jobs", "batches", "batch_jobs",
          "decisions", "mcp_attempts", "events")
MAX_PACK_MEMBER_BYTES = 256 * 1024 * 1024


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _records_for(conn, ids: list[str], portable: bool = False) -> list[dict]:
    records = []
    for table in TABLES:
        marks = ",".join("?" for _ in ids)
        if table == "batch_jobs":
            rows = conn.execute(
                f"SELECT bj.*,b.run_id AS _run_id FROM batch_jobs bj JOIN batches b "
                f"ON b.batch_id=bj.batch_id WHERE b.run_id IN ({marks})", ids,
            )
        else:
            rows = conn.execute(f"SELECT * FROM {table} WHERE run_id IN ({marks})", ids)
        for row in rows:
            value = dict(row)
            record_run_id = value.pop("_run_id", value.get("run_id", ""))
            if portable and table == "runs":
                value["report_dir"] = ""
            if table == "mcp_attempts": value.pop("attempt_id", None)
            if table == "events": value.pop("event_id", None)
            records.append({"table": table, "run_id": record_run_id, "row": value})
    order = {name: index for index, name in enumerate(TABLES)}
    records.sort(key=lambda item: (item.get("run_id", item["row"].get("run_id", "")),
                                   order[item["table"]], _canonical(item["row"])))
    return records


def export_pack(workspace: str, output: str, run_ids: list[str] | None = None) -> str:
    conn = connect(workspace)
    try:
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            runs = conn.execute(f"SELECT * FROM runs WHERE run_id IN ({marks})", run_ids).fetchall()
            found = {row["run_id"] for row in runs}
            missing = sorted(set(run_ids) - found)
            if missing:
                raise ValueError("指定的 Run 不存在: " + ",".join(missing))
        else:
            runs = conn.execute("SELECT * FROM runs WHERE status IN ('ALL_DONE','PARTIAL_DONE') AND origin='local'").fetchall()
        if not runs: raise ValueError("没有可导出的已结束本机运行")
        bad = [row["run_id"] for row in runs if row["status"] not in {"ALL_DONE", "PARTIAL_DONE"}]
        if bad: raise ValueError("运行中任务不能导出: " + ",".join(bad))
        ids = [row["run_id"] for row in runs]
        records = _records_for(conn, ids, portable=True)
        payload = b"\n".join(_canonical(record) for record in records) + b"\n"
        profile = get_profile(workspace)
        manifest = {"format": "xljpack", "schema_version": 1, "created_at": utc_now(),
                    "source_user_name": profile["display_name"], "source_device_id": profile["device_id"],
                    "run_ids": ids, "data_sha256": hashlib.sha256(payload).hexdigest()}
        target = os.path.abspath(output if output.lower().endswith(".xljpack") else output + ".xljpack")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zipfile.ZipFile(target + ".tmp", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", _canonical(manifest)); zf.writestr("data.jsonl", payload)
        os.replace(target + ".tmp", target); return target
    finally:
        conn.close()


def import_pack(workspace: str, source: str, commit: bool = False) -> dict:
    with open(source, "rb") as handle:
        raw = handle.read()
    package_hash = hashlib.sha256(raw).hexdigest()
    try:
        with zipfile.ZipFile(source) as zf:
            names = zf.namelist()
            if names.count("manifest.json") != 1 or names.count("data.jsonl") != 1 or len(names) != 2:
                raise ValueError("xljpack 必须且只能包含 manifest.json 和 data.jsonl")
            for name in names:
                info = zf.getinfo(name)
                if info.file_size > MAX_PACK_MEMBER_BYTES:
                    raise ValueError(f"xljpack 文件过大: {name}")
            manifest = json.loads(zf.read("manifest.json")); payload = zf.read("data.jsonl")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("xljpack 压缩包或 manifest 无效") from exc
    if manifest.get("format") != "xljpack" or manifest.get("schema_version") != 1:
        raise ValueError("不支持的 xljpack 格式")
    if hashlib.sha256(payload).hexdigest() != manifest.get("data_sha256"):
        raise ValueError("xljpack 数据校验失败")
    run_ids = manifest.get("run_ids")
    if (not isinstance(run_ids, list) or not run_ids or
            any(not isinstance(value, str) or not value for value in run_ids) or
            len(run_ids) != len(set(run_ids))):
        raise ValueError("xljpack manifest.run_ids 无效")
    try:
        records = [json.loads(line) for line in payload.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("xljpack data.jsonl 不是有效 UTF-8 JSONL") from exc
    if not records:
        raise ValueError("xljpack 不包含运行记录")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"table", "run_id", "row"}:
            raise ValueError("xljpack 记录结构无效")
        if record["table"] not in TABLES or not isinstance(record["row"], dict):
            raise ValueError("xljpack 包含不允许的表或行")
        if record["run_id"] not in run_ids:
            raise ValueError("xljpack 记录不属于 manifest 声明的 Run")
        if record["table"] != "batch_jobs" and record["row"].get("run_id") != record["run_id"]:
            raise ValueError("xljpack 行内 run_id 与外层不一致")
    incoming_runs = {r["row"]["run_id"]: r["row"] for r in records if r["table"] == "runs"}
    if set(incoming_runs) != set(run_ids) or sum(1 for r in records if r["table"] == "runs") != len(run_ids):
        raise ValueError("xljpack 的 Run 清单与数据不一致")
    if any(row.get("status") not in {"ALL_DONE", "PARTIAL_DONE"} for row in incoming_runs.values()):
        raise ValueError("xljpack 只能包含已结束运行")
    conn = connect(workspace)
    try:
        allowed_columns = {
            table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in TABLES
        }
        for record in records:
            if not set(record["row"]).issubset(allowed_columns[record["table"]]):
                raise ValueError(f"xljpack 表 {record['table']} 包含未知字段")
        analysis = {"new": [], "skipped": [], "conflicts": [], "committed": False, "package_hash": package_hash}
        for run_id, row in incoming_runs.items():
            local = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            incoming_hash = hashlib.sha256(_canonical([
                r for r in records if r.get("run_id", r["row"].get("run_id")) == run_id
            ])).hexdigest()
            local_hash = hashlib.sha256(_canonical(_records_for(conn, [run_id], portable=True))).hexdigest() if local else ""
            if not local: analysis["new"].append(run_id)
            elif local["content_hash"] == incoming_hash or local_hash == incoming_hash: analysis["skipped"].append(run_id)
            else: analysis["conflicts"].append(run_id)
        if not commit or analysis["conflicts"]:
            return analysis
        with transaction(conn):
            for run_id in analysis["new"]:
                run_records = [r for r in records if r.get("run_id", r["row"].get("run_id")) == run_id]
                incoming_hash = hashlib.sha256(_canonical(run_records)).hexdigest()
                for record in run_records:
                    row = dict(record["row"]); table = record["table"]
                    if table == "runs":
                        row["origin"] = "imported"; row["source_user_name"] = manifest.get("source_user_name", "")
                        row["source_device_id"] = manifest.get("source_device_id", ""); row["content_hash"] = incoming_hash
                    cols = list(row); marks = ",".join("?" for _ in cols)
                    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES({marks})", [row[c] for c in cols])
            conn.execute("INSERT OR REPLACE INTO sync_imports VALUES(?,?,?,?,?)", (
                package_hash, manifest.get("source_device_id", ""), utc_now(), "IMPORTED",
                json.dumps(analysis, ensure_ascii=False, sort_keys=True),
            ))
        analysis["committed"] = True; return analysis
    finally:
        conn.close()
