"""Workspace-scoped, content-addressed cache for reusable AI pipeline results."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .db import STORAGE


CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_LOCK = threading.RLock()


def fingerprint(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _scope_id(workspace_id: str | None) -> str:
    scope = str(workspace_id or "local-default").strip() or "local-default"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]


class LocalResultCache:
    def __init__(self, namespace: str, root: Path | None = None, ttl_seconds: int = CACHE_TTL_SECONDS):
        if not namespace or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in namespace):
            raise ValueError("cache namespace is invalid")
        self.root = Path(root or (STORAGE / "ai-result-cache")) / namespace
        self.ttl_seconds = max(1, int(ttl_seconds))

    def _path(self, workspace_id: str | None, cache_key: str) -> Path:
        if len(cache_key) != 64 or any(char not in "0123456789abcdef" for char in cache_key):
            raise ValueError("cache key must be a SHA-256 fingerprint")
        return self.root / _scope_id(workspace_id) / (cache_key + ".json")

    def get(self, workspace_id: str | None, cache_key: str, *, force_refresh: bool = False) -> tuple[bool, Any]:
        if force_refresh:
            return False, None
        path = self._path(workspace_id, cache_key)
        with _LOCK:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                created_at = float(record.get("created_at") or 0)
                if record.get("key") != cache_key or time.time() - created_at > self.ttl_seconds:
                    return False, None
                return True, record.get("value")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False, None

    def put(self, workspace_id: str | None, cache_key: str, value: Any) -> None:
        path = self._path(workspace_id, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        body = {"key": cache_key, "created_at": time.time(), "value": value}
        with _LOCK:
            temp.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, path)
