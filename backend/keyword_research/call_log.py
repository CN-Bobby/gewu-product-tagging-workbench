# -*- coding: utf-8 -*-
"""调用日志（skill 契约 cache/call_log.json）：调用前写 in_progress，返回后原位补全。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


class CallLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"calls": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"calls": []}

    def start_call(self, tool: str, scope: str, *, is_retry: bool = False,
                   retry_reason: str | None = None, planned_total: int | None = None,
                   hard_cap: int | None = None) -> int:
        """写入 in_progress 记录，返回 seq。"""
        with self._lock:
            data = self.load()
            calls = data.setdefault("calls", [])
            seq = len(calls) + 1
            calls.append({
                "seq": seq,
                "tool": tool,
                "scope": scope,
                "status": "in_progress",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "is_retry": is_retry,
                "retry_reason": retry_reason,
            })
            if planned_total is not None:
                data["planned_total"] = planned_total
            if hard_cap is not None:
                data["hard_cap"] = hard_cap
            self._write(data)
            return seq

    def finish_call(self, seq: int, *, success: bool, returned: Optional[int] = None,
                    truncated: bool = False, error: str | None = None) -> None:
        """原位补全一条调用记录。"""
        with self._lock:
            data = self.load()
            for call in data.get("calls", []):
                if call.get("seq") == seq:
                    call["status"] = "success" if success else "failed"
                    call["success"] = success
                    call["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    if returned is not None:
                        call["returned"] = returned
                    if truncated:
                        call["truncated"] = True
                    if error:
                        call["error"] = error
                    break
            else:
                # 找不到：追加一条（防御性，正常流程不应发生）
                data.setdefault("calls", []).append({
                    "seq": seq, "status": "success" if success else "failed",
                    "success": success, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                })
            completed = [c for c in data.get("calls", []) if c.get("status") != "in_progress"]
            data["actual_total"] = len(completed)
            self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def summary(self) -> dict[str, Any]:
        data = self.load()
        calls = data.get("calls", [])
        return {
            "planned_total": data.get("planned_total"),
            "hard_cap": data.get("hard_cap"),
            "actual_total": data.get("actual_total", len(calls)),
            "in_progress": [c for c in calls if c.get("status") == "in_progress"],
            "failed": [c for c in calls if c.get("status") == "failed"],
            "success_count": sum(1 for c in calls if c.get("status") == "success"),
        }
