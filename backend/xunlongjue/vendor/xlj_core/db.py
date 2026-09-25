"""SQLite schema, sequential migrations and transaction helpers."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 4
DB_FILENAME = "xunlongjue.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return os.path.join(base, "Xunlongjue", "v4", "data")


def default_report_dir() -> str:
    return str(Path.home() / "Documents" / "WorkBuddy" / "寻龙诀v4工作区" / "报告")


def database_path(workspace: str | None = None) -> str:
    return os.path.join(os.path.abspath(workspace or default_data_dir()), DB_FILENAME)


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, site TEXT NOT NULL, root_node_id TEXT NOT NULL,
 category TEXT NOT NULL, policy_version TEXT NOT NULL, policy_json TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 completed_at TEXT, operator_name TEXT NOT NULL DEFAULT '', operator_id TEXT NOT NULL DEFAULT '',
 device_id TEXT NOT NULL DEFAULT '', timezone TEXT NOT NULL DEFAULT '',
 origin TEXT NOT NULL DEFAULT 'local', source_user_name TEXT NOT NULL DEFAULT '',
 source_device_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
 report_dir TEXT NOT NULL DEFAULT '', owner_id TEXT NOT NULL DEFAULT '',
 owner_label TEXT NOT NULL DEFAULT '', owner_heartbeat TEXT
);
CREATE TABLE IF NOT EXISTS nodes(
 run_id TEXT NOT NULL, node_id TEXT NOT NULL, path TEXT NOT NULL, level INTEGER NOT NULL,
 state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error_reason TEXT NOT NULL DEFAULT '',
 history_months INTEGER NOT NULL DEFAULT 0, current_scale REAL,
 descendants_known INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(run_id,node_id), FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS edges(
 run_id TEXT NOT NULL,parent_id TEXT NOT NULL,child_id TEXT NOT NULL,child_name TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(run_id,parent_id,child_id),
 FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS observations(
 run_id TEXT NOT NULL,node_id TEXT NOT NULL,indicator TEXT NOT NULL,month TEXT NOT NULL,
 value REAL NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,node_id,indicator,month),
 FOREIGN KEY(run_id,node_id) REFERENCES nodes(run_id,node_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS jobs(
 job_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT NOT NULL,job_type TEXT NOT NULL,
 indicator TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 lease_until TEXT,lease_token TEXT NOT NULL DEFAULT '',eligible_at TEXT,
 last_error TEXT NOT NULL DEFAULT '',result_hash TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(run_id,node_id,job_type,indicator),
 FOREIGN KEY(run_id,node_id) REFERENCES nodes(run_id,node_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ingest_receipts(
 job_id TEXT NOT NULL,receipt_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,receipt_hash),FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS batches(
 batch_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,worker_id TEXT NOT NULL,state TEXT NOT NULL,
 relative_dir TEXT NOT NULL,job_count INTEGER NOT NULL,lease_until TEXT,
 completed_jobs INTEGER NOT NULL DEFAULT 0,failed_jobs INTEGER NOT NULL DEFAULT 0,
 heartbeat_at TEXT,current_node_id TEXT NOT NULL DEFAULT '',stage TEXT NOT NULL DEFAULT 'LEASED',
 receipt_hash TEXT NOT NULL DEFAULT '',receipt_json TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,ingested_at TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS batch_jobs(
 batch_id TEXT NOT NULL,job_id TEXT NOT NULL,attempt INTEGER NOT NULL,lease_token TEXT NOT NULL,
 state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(batch_id,job_id),FOREIGN KEY(batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
 FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decisions(
 run_id TEXT NOT NULL,node_id TEXT NOT NULL,status TEXT NOT NULL,direction TEXT NOT NULL,
 sales_slope REAL,price_slope REAL,yoy_sales REAL,yoy_price REAL,avg_sales REAL,avg_price REAL,
 sales_r2 REAL,price_r2 REAL,current_scale REAL,projected_scale REAL,
 opportunity_level TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
 policy_version TEXT NOT NULL,decided_at TEXT NOT NULL,PRIMARY KEY(run_id,node_id),
 FOREIGN KEY(run_id,node_id) REFERENCES nodes(run_id,node_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS mcp_attempts(
 attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,job_id TEXT,node_id TEXT NOT NULL,
 site TEXT NOT NULL,call_type TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
 data_count INTEGER NOT NULL DEFAULT 0,attempt_no INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,event_type TEXT NOT NULL,
 node_id TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
 UNIQUE(run_id,event_type,node_id,detail),FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS local_profile(
 profile_id INTEGER PRIMARY KEY CHECK(profile_id=1),user_id TEXT NOT NULL,display_name TEXT NOT NULL,
 device_id TEXT NOT NULL,device_name TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_requests(
 request_code TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,
 site TEXT NOT NULL,root_node_id TEXT NOT NULL,category TEXT NOT NULL,policy_json TEXT NOT NULL,
 report_dir TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,claimed_at TEXT,
 run_id TEXT,cloned_from TEXT NOT NULL DEFAULT '',FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS parameter_presets(
 preset_id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,description TEXT NOT NULL DEFAULT '',
 site TEXT NOT NULL,root_node_id TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT '',
 policy_json TEXT NOT NULL,report_dir TEXT NOT NULL DEFAULT '',is_default INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_imports(
 package_hash TEXT PRIMARY KEY,source_device_id TEXT NOT NULL,imported_at TEXT NOT NULL,
 status TEXT NOT NULL,summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_artifacts(
 run_id TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,kind,path),FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: list[str]) -> None:
    existing = _columns(conn, table)
    for definition in definitions:
        name = definition.split()[0]
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(BASE_SCHEMA)
    # Existing beta.1 databases are upgraded in place; every ALTER is idempotent.
    _add_columns(conn, "runs", [
        "operator_name TEXT NOT NULL DEFAULT ''", "operator_id TEXT NOT NULL DEFAULT ''",
        "device_id TEXT NOT NULL DEFAULT ''", "timezone TEXT NOT NULL DEFAULT ''",
        "origin TEXT NOT NULL DEFAULT 'local'", "source_user_name TEXT NOT NULL DEFAULT ''",
        "source_device_id TEXT NOT NULL DEFAULT ''", "content_hash TEXT NOT NULL DEFAULT ''",
        "report_dir TEXT NOT NULL DEFAULT ''",
        "owner_id TEXT NOT NULL DEFAULT ''", "owner_label TEXT NOT NULL DEFAULT ''",
        "owner_heartbeat TEXT",
    ])
    conn.execute(
        "UPDATE runs SET owner_id=lower(hex(randomblob(16))),"
        "owner_label=CASE WHEN owner_label='' THEN 'WorkBuddy/' || operator_name ELSE owner_label END,"
        "owner_heartbeat=COALESCE(owner_heartbeat,updated_at) WHERE owner_id=''"
    )
    _add_columns(conn, "nodes", [
        "history_months INTEGER NOT NULL DEFAULT 0", "current_scale REAL",
        "descendants_known INTEGER NOT NULL DEFAULT 0",
    ])
    _add_columns(conn, "jobs", [
        "lease_token TEXT NOT NULL DEFAULT ''", "eligible_at TEXT",
    ])
    _add_columns(conn, "decisions", ["current_scale REAL", "projected_scale REAL"])
    _add_columns(conn, "mcp_attempts", ["attempt_no INTEGER NOT NULL DEFAULT 0"])
    _add_columns(conn, "batches", [
        "completed_jobs INTEGER NOT NULL DEFAULT 0", "failed_jobs INTEGER NOT NULL DEFAULT 0",
        "heartbeat_at TEXT", "current_node_id TEXT NOT NULL DEFAULT ''",
        "stage TEXT NOT NULL DEFAULT 'LEASED'",
    ])
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_nodes_state ON nodes(run_id,state);
    CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(run_id,state,eligible_at,created_at);
    CREATE INDEX IF NOT EXISTS idx_attempts_run ON mcp_attempts(run_id,attempt_id);
    CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id,event_id);
    CREATE INDEX IF NOT EXISTS idx_batches_run_state ON batches(run_id,state,created_at);
    CREATE INDEX IF NOT EXISTS idx_batch_jobs_job ON batch_jobs(job_id,state);
    CREATE INDEX IF NOT EXISTS idx_presets_default ON parameter_presets(is_default,updated_at);
    """)
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (utc_now(),))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (utc_now(),))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)", (utc_now(),))
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(4,?)", (utc_now(),))


def connect(workspace: str | None = None) -> sqlite3.Connection:
    data_dir = os.path.abspath(workspace or default_data_dir())
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(database_path(data_dir), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    has_migrations = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    current_schema = 0
    if has_migrations:
        row = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
        current_schema = int(row[0])
    if current_schema < SCHEMA_VERSION:
        _migrate(conn)
        conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def event(conn: sqlite3.Connection, run_id: str, event_type: str,
          node_id: str = "", detail: object = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    if event_type == "DEPTH_COMPLETED" and conn.execute(
        "SELECT 1 FROM events WHERE run_id=? AND event_type=? AND node_id=?",
        (run_id, event_type, str(node_id or "")),
    ).fetchone():
        return
    conn.execute(
        "INSERT OR IGNORE INTO events(run_id,event_type,node_id,detail,created_at) VALUES(?,?,?,?,?)",
        (run_id, event_type, str(node_id or ""), detail, utc_now()),
    )


def require_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"运行不存在: {run_id}")
    return row
