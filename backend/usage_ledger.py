"""Unified local usage ledger; vendor charges stay unknown unless provided by a provider."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .db import STORAGE


_LOCK = threading.RLock()
_KNOWN_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_hit_input_tokens",
                       "cache_miss_input_tokens", "review_api_calls", "raw_records",
                       "new_unique_comments", "retries")


def _scope_id(workspace_id: str | None) -> str:
    scope = str(workspace_id or "local-default").strip() or "local-default"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]


def _path(task_type: str, task_id: str, workspace_id: str | None) -> Path:
    if task_type not in ("reviews", "voc") or not task_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in task_id.casefold()):
        raise ValueError("usage ledger identity is invalid")
    return STORAGE / "usage-ledger" / _scope_id(workspace_id) / task_type / (task_id + ".json")


def record_usage(task_type: str, task_id: str, workspace_id: str | None, entry: dict[str, Any]) -> None:
    """Append metadata only; do not pass source text, prompts, or credentials."""
    path = _path(task_type, task_id, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(current, dict) or not isinstance(current.get("entries"), list):
                current = {"schema": "workbench-usage-ledger/v1", "task_type": task_type,
                           "task_id": task_id, "workspace_id": workspace_id, "entries": []}
        except (OSError, ValueError, json.JSONDecodeError):
            current = {"schema": "workbench-usage-ledger/v1", "task_type": task_type,
                       "task_id": task_id, "workspace_id": workspace_id, "entries": []}
        clean = {key: value for key, value in entry.items() if key not in ("prompt", "payload", "api_key", "credential")}
        clean.setdefault("record_id", uuid.uuid4().hex)
        clean.setdefault("recorded_at", time.time())
        for key in _KNOWN_USAGE_FIELDS:
            clean.setdefault(key, None)
        current["entries"].append(clean)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)


def usage_summary(task_type: str, task_id: str, workspace_id: str | None) -> dict[str, Any]:
    path = _path(task_type, task_id, workspace_id)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc.get("entries", []) if isinstance(doc, dict) else []
    except (OSError, ValueError, json.JSONDecodeError):
        entries = []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault((str(entry.get("stage") or "unknown"), str(entry.get("model") or "")), []).append(entry)

    def total(rows: list[dict[str, Any]], field: str):
        values = [row.get(field) for row in rows if isinstance(row.get(field), (int, float))]
        return sum(values) if values else None

    stages = []
    for (stage, model), rows in sorted(grouped.items()):
        stages.append({
            "stage": stage,
            "model": model,
            "requests": sum(1 for row in rows if row.get("request") is True),
            "successful_requests": sum(1 for row in rows if row.get("status") == "completed"),
            "failed_requests": sum(1 for row in rows if row.get("status") == "failed"),
            "local_cache_hits": sum(1 for row in rows if row.get("local_cache_hit") is True),
            "local_cache_misses": sum(1 for row in rows if row.get("local_cache_miss") is True),
            **{field: total(rows, field) for field in _KNOWN_USAGE_FIELDS},
            "cache_usage_missing_requests": sum(1 for row in rows
                if row.get("request") is True and row.get("cache_hit_input_tokens") is None
                and row.get("cache_miss_input_tokens") is None),
            "cost": None,
            "cost_status": "unknown",
        })
    return {"schema": "workbench-usage-summary/v1", "task_type": task_type, "task_id": task_id,
            "workspace_id": workspace_id, "entries": len(entries), "stages": stages,
            "cost": None, "cost_status": "unknown"}
