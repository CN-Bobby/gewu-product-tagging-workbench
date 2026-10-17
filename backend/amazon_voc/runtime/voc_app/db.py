from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, marketplace TEXT NOT NULL,
  country TEXT NOT NULL, language TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
  workspace_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), status TEXT NOT NULL,
  current_revision INTEGER NOT NULL DEFAULT 1, source_json TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT NOT NULL DEFAULT '', started_at TEXT, completed_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), revision INTEGER NOT NULL,
  stage TEXT NOT NULL, batch_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  status TEXT NOT NULL, output_path TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, UNIQUE(run_id, revision, stage, batch_id)
);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), revision INTEGER NOT NULL,
  stage TEXT NOT NULL, batch_id TEXT NOT NULL, input_hash TEXT NOT NULL, model TEXT NOT NULL,
  prompt_version TEXT NOT NULL, protocol TEXT NOT NULL, latency_ms INTEGER NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_hit_input_tokens INTEGER, cache_miss_input_tokens INTEGER,
  retries INTEGER NOT NULL, status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
  from_revision INTEGER NOT NULL, to_revision INTEGER NOT NULL, event_id TEXT NOT NULL,
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), revision INTEGER NOT NULL,
  kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(run_id, revision, kind)
);
"""

_UNSET = object()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "workspace_id" not in project_columns:
                conn.execute("ALTER TABLE projects ADD COLUMN workspace_id TEXT")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(model_calls)").fetchall()}
            if "cache_hit_input_tokens" not in columns:
                conn.execute("ALTER TABLE model_calls ADD COLUMN cache_hit_input_tokens INTEGER")
            if "cache_miss_input_tokens" not in columns:
                conn.execute("ALTER TABLE model_calls ADD COLUMN cache_miss_input_tokens INTEGER")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def execute(self, sql, params=()):
        with self._lock, self.connect() as conn:
            return conn.execute(sql, params).rowcount

    def one(self, sql, params=()):
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def record_model_call(self, run_id, revision, stage, batch_id, input_hash, model,
                          prompt_version, protocol, latency_ms, input_tokens, output_tokens,
                          retries, status, error="", created_at=None,
                          cache_hit_input_tokens=None, cache_miss_input_tokens=None):
        """Insert a model call; cache usage is optional for older callers/providers."""
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO model_calls(run_id,revision,stage,batch_id,input_hash,model,prompt_version,protocol,"
                "latency_ms,input_tokens,output_tokens,cache_hit_input_tokens,cache_miss_input_tokens,"
                "retries,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, revision, stage, batch_id, input_hash, model, prompt_version, protocol,
                 latency_ms, input_tokens, output_tokens, cache_hit_input_tokens,
                 cache_miss_input_tokens, retries, status, error, created_at or utcnow()))
            return cursor.lastrowid

    def update_model_call(self, call_id, *, cache_hit_input_tokens=_UNSET,
                          cache_miss_input_tokens=_UNSET, **fields):
        """Update call usage fields while accepting the optional cache fields by keyword."""
        allowed = {"input_tokens", "output_tokens", "cache_hit_input_tokens",
                   "cache_miss_input_tokens", "latency_ms", "retries", "status", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        # Omitted fields preserve existing values; explicit None records unknown.
        if cache_hit_input_tokens is not _UNSET:
            updates["cache_hit_input_tokens"] = cache_hit_input_tokens
        if cache_miss_input_tokens is not _UNSET:
            updates["cache_miss_input_tokens"] = cache_miss_input_tokens
        if not updates:
            return 0
        assignments = ",".join("%s=?" % key for key in updates)
        with self._lock, self.connect() as conn:
            return conn.execute("UPDATE model_calls SET %s WHERE id=?" % assignments,
                                tuple(updates.values()) + (call_id,)).rowcount

    def create_project(self, name, marketplace, country, language, category, workspace_id=None):
        project_id = "prj_" + uuid.uuid4().hex[:12]
        now = utcnow()
        self.execute("INSERT INTO projects(id,name,marketplace,country,language,category,workspace_id,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (project_id, name, marketplace, country, language, category, workspace_id, now, now))
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def create_run(self, project_id, sources):
        run_id = "run_" + uuid.uuid4().hex[:12]
        now = utcnow()
        self.execute("INSERT INTO runs(id,project_id,status,source_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (run_id, project_id, "uploaded", json.dumps(sources, ensure_ascii=False), now, now))
        return self.get_run(run_id)

    def get_run(self, run_id):
        row = self.one("SELECT r.*,p.name project_name,p.marketplace,p.country,p.language,p.category,p.workspace_id "
                       "FROM runs r JOIN projects p ON p.id=r.project_id WHERE r.id=?", (run_id,))
        if row:
            row["sources"] = json.loads(row.pop("source_json"))
        return row

    def update_run(self, run_id, **fields):
        if not fields:
            return
        fields["updated_at"] = utcnow()
        names = list(fields)
        self.execute("UPDATE runs SET %s WHERE id=?" % ",".join("%s=?" % n for n in names),
                     tuple(fields[n] for n in names) + (run_id,))

    def list_projects(self):
        return self.all("SELECT p.*,COUNT(r.id) run_count,MAX(r.updated_at) last_run_at "
                        "FROM projects p LEFT JOIN runs r ON r.project_id=p.id GROUP BY p.id ORDER BY p.updated_at DESC")

    def list_runs(self, project_id=None):
        sql = "SELECT * FROM runs" + (" WHERE project_id=?" if project_id else "") + " ORDER BY created_at DESC"
        rows = self.all(sql, (project_id,) if project_id else ())
        for row in rows:
            row["sources"] = json.loads(row.pop("source_json"))
        return rows

    def pending_run(self, worker_id, allowed_run_ids=None):
        if allowed_run_ids is not None:
            allowed_run_ids = tuple(dict.fromkeys(str(run_id) for run_id in allowed_run_ids if run_id))
            if not allowed_run_ids:
                return None
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
            scope_sql = ""
            params = []
            if allowed_run_ids is not None:
                scope_sql = "id IN (%s) AND " % ",".join("?" for _ in allowed_run_ids)
                params.extend(allowed_run_ids)
            params.append(stale)
            row = conn.execute("SELECT id FROM runs WHERE " + scope_sql +
                               "cancel_requested=0 AND status NOT IN "
                               "('completed','failed','cancelled') AND (worker_id='' OR updated_at<?) "
                               "ORDER BY updated_at LIMIT 1", tuple(params)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE runs SET worker_id=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                         (worker_id, utcnow(), utcnow(), row["id"]))
            return row["id"]
