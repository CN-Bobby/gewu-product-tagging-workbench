#!/usr/bin/env python3
"""Copy verified legacy deliverables into their owning project folders.

The default mode is read-only. Use --apply only with an explicit storage root;
the source artifact is never moved or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FOLDERS = {
    "source": {"dataset": {".xlsx"}},
    "data_x": {"data_x_snapshot": {".xlsx"}},
    "reviews": {
        "amazon_reviews": {".xlsx"},
        "sorftime_mcp_review_export": {".xlsx"},
    },
    "voc": {"amazon_voc_output": {".html"}},
}
ISSUE_STATUSES = {
    "source_missing", "source_not_regular_file", "source_unreadable",
    "source_hash_mismatch", "existing_copy_unverified",
    "source_changed_before_copy", "copy_hash_mismatch",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _visible(row: dict[str, Any]) -> bool:
    kind = row.get("kind")
    source_type = row.get("source_type")
    extension = Path(str(row.get("name") or "")).suffix.casefold()
    if extension not in FOLDERS.get(str(kind), {}).get(str(source_type), set()):
        return False
    if kind == "reviews" and source_type == "amazon_reviews":
        return str(row.get("source_id") or "").endswith(":result.xlsx")
    return True


def _read_registry(storage: Path) -> list[dict[str, Any]]:
    database = storage / "workbench.db"
    if not database.is_file():
        raise ValueError(f"找不到工作台数据库：{database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(project_artifact)")}
            required = {"id", "workspace_id", "kind", "source_type", "source_id", "name",
                        "relative_path", "sha256", "size", "metadata_json", "created_at"}
            if not required <= columns:
                raise ValueError("项目文件登记表缺少必要字段")
            rows = connection.execute(
                "SELECT id, workspace_id, kind, source_type, source_id, name, relative_path, "
                "sha256, size, metadata_json, created_at FROM project_artifact ORDER BY created_at, id"
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise ValueError(f"无法只读检查工作台数据库：{exc}") from exc


def _safe_project_file(storage: Path, workspace_id: str, relative_path: str) -> Path | None:
    if not workspace_id or len(workspace_id) != 32 or any(ch not in "0123456789abcdef" for ch in workspace_id):
        return None
    if not relative_path or relative_path.startswith("external:"):
        return None
    root = (storage / "projects" / workspace_id).resolve()
    candidate = (root / Path(relative_path)).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _collect(storage: Path) -> list[dict[str, Any]]:
    registry = _read_registry(storage)
    copied_by_key: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in registry:
        if row["relative_path"].startswith("external:") or not _visible(row):
            continue
        key = (row["workspace_id"], row["kind"], row["source_type"], row["source_id"], row["sha256"])
        copied_by_key.setdefault(key, []).append(row)

    results = []
    for row in registry:
        if not row["relative_path"].startswith("external:") or not _visible(row):
            continue
        key = (row["workspace_id"], row["kind"], row["source_type"], row["source_id"], row["sha256"])
        existing = copied_by_key.get(key, [])
        if existing:
            verified = any(
                (path := _safe_project_file(storage, row["workspace_id"], item["relative_path"])) is not None
                and _sha256(path) == row["sha256"]
                for item in existing
            )
            status = "already_backfilled" if verified else "existing_copy_unverified"
            results.append(_report_item(row, status))
            continue

        source_value = str(row["relative_path"])[len("external:"):]
        source = Path(source_value).expanduser()
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            results.append(_report_item(row, "source_not_regular_file"))
            continue
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError):
            results.append(_report_item(row, "source_missing"))
            continue
        if not source.is_file():
            results.append(_report_item(row, "source_not_regular_file"))
            continue
        try:
            actual = _sha256(source)
        except OSError:
            results.append(_report_item(row, "source_unreadable"))
            continue
        if actual != row["sha256"]:
            results.append(_report_item(row, "source_hash_mismatch", actual_sha256=actual))
            continue
        results.append(_report_item(row, "ready", actual_sha256=actual))
    return results


def _report_item(row: dict[str, Any], status: str, *, actual_sha256: str | None = None) -> dict[str, Any]:
    return {
        "artifact_id": row["id"], "workspace_id": row["workspace_id"],
        "kind": row["kind"], "source_type": row["source_type"],
        "source_id": row["source_id"], "name": row["name"],
        "expected_sha256": row["sha256"], "actual_sha256": actual_sha256,
        "status": status,
    }


def _apply(storage: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    os.environ["TAG_STORAGE"] = str(storage)
    sys.path.insert(0, str(ROOT))
    from backend import project_files

    registry = {row["id"]: row for row in _read_registry(storage)}
    for entry in entries:
        if entry["status"] != "ready":
            continue
        row = registry[entry["artifact_id"]]
        source = Path(str(row["relative_path"])[len("external:"):]).expanduser().resolve(strict=True)
        actual = _sha256(source)
        if actual != row["sha256"]:
            entry["actual_sha256"] = actual
            entry["status"] = "source_changed_before_copy"
            continue
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        copied = project_files.register_file(
            row["workspace_id"], row["kind"], source,
            source_type=row["source_type"], source_id=row["source_id"],
            name=row["name"], metadata=metadata,
        )
        if copied["sha256"] != row["sha256"]:
            entry["status"] = "copy_hash_mismatch"
            continue
        entry["status"] = "backfilled"
        entry["copied_artifact_id"] = copied["id"]
    return entries


def _write_report(path: Path, payload: dict[str, Any], storage: Path) -> None:
    destination = path.expanduser().resolve()
    if destination.is_relative_to(storage.resolve()):
        raise ValueError("报告必须保存在项目数据目录之外")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", required=True, type=Path,
                        help="明确指定要检查的工作台数据目录")
    parser.add_argument("--apply", action="store_true",
                        help="复制通过来源和 SHA-256 核验的目标成品；默认只读预览")
    parser.add_argument("--report", type=Path,
                        help="可选 JSON 报告路径，必须位于数据目录之外")
    args = parser.parse_args(argv)
    if args.apply and not args.report:
        parser.error("执行补齐时必须指定 --report 保存核验清单")
    storage = args.storage.expanduser().resolve()
    if not storage.is_dir():
        parser.error("数据目录不存在")
    try:
        entries = _collect(storage)
        if args.apply:
            entries = _apply(storage, entries)
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        payload = {
            "schema_version": 1,
            "mode": "apply" if args.apply else "dry_run",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts,
            "items": entries,
        }
        if args.report:
            _write_report(args.report, payload, storage)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2 if ISSUE_STATUSES.intersection(counts) else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
