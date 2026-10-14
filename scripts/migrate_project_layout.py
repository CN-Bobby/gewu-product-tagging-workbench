"""Safely migrate DEV project files to the canonical project-library layout.

The command is read-only by default. Applying it requires an explicit flag and a
data directory that exactly matches the Run Center DEV profile. Stop DEV through
Run Center before using --apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

VISIBLE_FOLDERS = {
    "source": "source",
    "data_x": "data-x",
    "reviews": "reviews",
    "voc": "voc",
    "keywords": "keywords",
    "other": "other",
}
SYSTEM_FOLDERS = {
    "product_archive": "_system/product-archive",
    "images": "_system/images",
    "reports": "_system/reports",
    "legacy": "_system/legacy-index",
}
FOLDERS = {**VISIBLE_FOLDERS, **SYSTEM_FOLDERS}
TRASH_FOLDER = "_system/trash"
_WORKSPACE_ID = re.compile(r"^[a-f0-9]{32}$")
OLD_FOLDER_MAP = {
    "source": "source",
    "data-x": "data-x",
    "reviews": "reviews",
    "voc": "voc",
    "keywords": "keywords",
    "other": "other",
    "product-archive": "_system/product-archive",
    "images": "_system/images",
    "reports": "_system/reports",
    "legacy-index": "_system/legacy-index",
}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileMove:
    workspace_id: str
    source: Path
    destination: Path
    sha256: str
    size: int


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _relative(root: Path, value: str) -> PurePosixPath:
    if not value or value.startswith("external:"):
        raise MigrationError("外部引用没有项目内相对路径")
    normalized = value.replace("\\", "/")
    rel = PurePosixPath(normalized)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise MigrationError(f"项目相对路径无效：{value}")
    candidate = root.joinpath(*rel.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise MigrationError(f"路径越过项目目录：{value}") from exc
    return rel


def _registered_destination(kind: str, relative_path: str, metadata: dict[str, Any],
                            root: Path) -> str:
    if kind not in FOLDERS:
        raise MigrationError(f"未知文件分类：{kind}")
    rel = _relative(root, relative_path)
    filename = rel.name
    if metadata.get("deleted_at"):
        return f"{TRASH_FOLDER}/{filename}"
    return f"{FOLDERS[kind]}/{filename}"


def _unregistered_destination(relative_path: str, root: Path) -> str:
    rel = _relative(root, relative_path)
    parts = rel.parts
    if parts[0] == "_system":
        return rel.as_posix()
    if parts[0] == ".trash":
        suffix = parts[1:]
        if not suffix:
            raise MigrationError("回收站目录本身不能作为文件迁移")
        return PurePosixPath(TRASH_FOLDER, *suffix).as_posix()
    if parts[0] == "source" and len(parts) >= 2 and parts[1] == "raw":
        suffix = parts[2:]
        if not suffix:
            raise MigrationError("来源文件 raw 目录本身不能作为文件迁移")
        return PurePosixPath("source", *suffix).as_posix()
    target_root = OLD_FOLDER_MAP.get(parts[0])
    if target_root is None:
        raise MigrationError(f"无法识别的项目文件路径：{relative_path}")
    return PurePosixPath(target_root, *parts[1:]).as_posix()


def _checked_file(root: Path, relative: str) -> Path:
    rel = _relative(root, relative)
    path = root.joinpath(*rel.parts)
    if _is_link(path):
        raise MigrationError(f"拒绝迁移链接文件：{relative}")
    if not path.is_file():
        raise MigrationError(f"登记文件不存在：{relative}")
    return path


def _load_rows(connection: sqlite3.Connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"workspace", "project_artifact"}.issubset(tables):
        raise MigrationError("数据库缺少项目资料表")
    workspace_ids = [row[0] for row in connection.execute("SELECT id FROM workspace ORDER BY id")]
    artifacts = connection.execute(
        "SELECT id, workspace_id, kind, relative_path, sha256, size, metadata_json FROM project_artifact"
    ).fetchall()
    return workspace_ids, artifacts


def build_plan(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.resolve(strict=True)
    projects_dir = data_dir / "projects"
    database = data_dir / "workbench.db"
    if _is_link(data_dir) or not projects_dir.is_dir() or _is_link(projects_dir):
        raise MigrationError("DEV 数据目录或 projects 目录无效/为链接")
    if not database.is_file() or _is_link(database):
        raise MigrationError("DEV 数据目录中缺少普通 workbench.db")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"数据库完整性检查失败：{integrity}")
        workspace_ids, artifact_rows = _load_rows(connection)
    finally:
        connection.close()

    directories = {path.name: path for path in projects_dir.iterdir() if path.is_dir()}
    root_files = [path.name for path in projects_dir.iterdir() if path.is_file()]
    if root_files:
        raise MigrationError("projects 根目录存在未归属文件")
    if any(not _WORKSPACE_ID.fullmatch(workspace_id) for workspace_id in workspace_ids):
        raise MigrationError("数据库包含无效项目编号")
    if set(directories) - set(workspace_ids):
        raise MigrationError("projects 目录包含数据库中不存在的项目")
    missing_directories = set(workspace_ids) - set(directories)

    by_workspace: dict[str, list[sqlite3.Row]] = {wid: [] for wid in workspace_ids}
    for row in artifact_rows:
        if row["workspace_id"] not in by_workspace:
            raise MigrationError(f"资料记录引用了不存在的项目：{row['workspace_id']}")
        by_workspace[row["workspace_id"]].append(row)

    moves: dict[tuple[str, str], FileMove] = {}
    target_hashes: dict[tuple[str, str], str] = {}
    record_updates: list[dict[str, Any]] = []
    conflicts: list[str] = []
    blocked_projects: set[str] = set()
    external_count = 0
    blocked_projects.update(missing_directories)
    conflicts.extend(f"{workspace_id}: 数据库项目缺少 projects 子目录"
                     for workspace_id in sorted(missing_directories))
    for workspace_id in workspace_ids:
        if workspace_id in missing_directories:
            continue
        if _is_link(directories[workspace_id]):
            conflicts.append(f"{workspace_id}: 项目根目录是链接")
            blocked_projects.add(workspace_id)
            continue
        root = directories[workspace_id].resolve(strict=True)
        registered_paths: set[str] = set()
        for row in by_workspace[workspace_id]:
            current = str(row["relative_path"] or "")
            if current.startswith("external:"):
                external_count += 1
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
                if not isinstance(metadata, dict):
                    raise MigrationError("资料 metadata 不是对象")
                source = _checked_file(root, current)
                digest, size = _sha256(source)
                if row["sha256"] and row["sha256"] != digest:
                    raise MigrationError("源文件 SHA-256 与资料记录不一致")
                if row["size"] is not None and int(row["size"]) != size:
                    raise MigrationError("源文件大小与资料记录不一致")
                destination_rel = _registered_destination(row["kind"], current, metadata, root)
                destination = root.joinpath(*PurePosixPath(destination_rel).parts)
                target_key = (workspace_id, destination_rel.casefold())
                previous_digest = target_hashes.get(target_key)
                if previous_digest and previous_digest != digest:
                    raise MigrationError(f"多个资料映射到同一目标且内容不同：{destination_rel}")
                target_hashes[target_key] = digest
                move_key = (workspace_id, source.relative_to(root).as_posix().casefold())
                if source.resolve() != destination.resolve():
                    moves.setdefault(move_key, FileMove(workspace_id, source, destination, digest, size))
                registered_paths.add(source.relative_to(root).as_posix().casefold())
                updated_metadata = dict(metadata)
                if metadata.get("deleted_at") and metadata.get("restore_path"):
                    restore_rel = str(metadata["restore_path"])
                    updated_metadata["restore_path"] = _registered_destination(
                        row["kind"], restore_rel, {}, root)
                if current != destination_rel or updated_metadata != metadata:
                    record_updates.append({
                        "id": row["id"], "workspace_id": workspace_id,
                        "relative_path": destination_rel,
                        "metadata_json": json.dumps(updated_metadata, ensure_ascii=False),
                    })
            except (MigrationError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                conflicts.append(f"{workspace_id}/{current}: {exc}")
                blocked_projects.add(workspace_id)

        # Include unregistered files in the physical migration. This preserves any
        # manually placed files that are not represented by a ProjectArtifact row.
        for path in root.rglob("*"):
            if _is_link(path):
                conflicts.append(f"{workspace_id}/{path.relative_to(root)}: 发现链接")
                blocked_projects.add(workspace_id)
                continue
            rel = path.relative_to(root).as_posix()
            if not path.is_file():
                if path.is_dir() and path != root and rel.split("/", 1)[0] not in {
                    "source", "data-x", "reviews", "voc", "keywords", "other",
                    "product-archive", "images", "reports", "legacy-index", ".trash", "_system",
                }:
                    conflicts.append(f"{workspace_id}/{rel}: 无法识别的目录")
                    blocked_projects.add(workspace_id)
                continue
            if rel.casefold() in registered_paths:
                continue
            try:
                destination_rel = _unregistered_destination(rel, root)
                digest, size = _sha256(path)
                destination = root.joinpath(*PurePosixPath(destination_rel).parts)
                target_key = (workspace_id, destination_rel.casefold())
                previous_digest = target_hashes.get(target_key)
                if previous_digest and previous_digest != digest:
                    raise MigrationError(f"多个文件映射到同一目标且内容不同：{destination_rel}")
                target_hashes[target_key] = digest
                move_key = (workspace_id, rel.casefold())
                if path.resolve() != destination.resolve():
                    moves.setdefault(move_key, FileMove(workspace_id, path, destination, digest, size))
            except (MigrationError, OSError, ValueError) as exc:
                conflicts.append(f"{workspace_id}/{rel}: {exc}")
                blocked_projects.add(workspace_id)

    for move in moves.values():
        if move.destination.exists() and move.destination.resolve() != move.source.resolve():
            try:
                digest, _ = _sha256(move.destination)
            except OSError as exc:
                conflicts.append(f"{move.workspace_id}/{move.destination}: 无法读取目标：{exc}")
                blocked_projects.add(move.workspace_id)
                continue
            if digest != move.sha256:
                conflicts.append(f"{move.workspace_id}/{move.destination}: 目标文件已存在且 SHA-256 不同")
                blocked_projects.add(move.workspace_id)

    return {
        "data_dir": str(data_dir),
        "project_count": len(workspace_ids),
        "registered_artifact_count": len(artifact_rows),
        "external_reference_count": external_count,
        "blocked_project_ids": sorted(blocked_projects),
        "file_move_count": len(moves),
        "record_update_count": len(record_updates),
        "conflicts": conflicts,
        "moves": list(moves.values()),
        "record_updates": record_updates,
    }


def _copy_verified(move: FileMove) -> None:
    if move.source.resolve() == move.destination.resolve():
        return
    move.destination.parent.mkdir(parents=True, exist_ok=True)
    if move.destination.exists():
        digest, size = _sha256(move.destination)
        if digest != move.sha256 or size != move.size:
            raise MigrationError(f"目标已存在且校验不同：{move.destination}")
        return
    temporary = move.destination.with_name(move.destination.name + f".{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(move.source, temporary)
        digest, size = _sha256(temporary)
        if digest != move.sha256 or size != move.size:
            raise MigrationError(f"复制后 SHA-256 校验失败：{move.source}")
        try:
            descriptor = os.open(move.destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            digest, size = _sha256(move.destination)
            if digest != move.sha256 or size != move.size:
                raise MigrationError(f"迁移期间目标发生冲突：{move.destination}")
            return
        os.close(descriptor)
        os.replace(temporary, move.destination)
    finally:
        temporary.unlink(missing_ok=True)


def _backup(data_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = data_dir / "backups" / f"project-layout-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    source_db = data_dir / "workbench.db"
    with sqlite3.connect(source_db) as source, sqlite3.connect(backup / "workbench.db") as target:
        source.backup(target)
    shutil.copytree(data_dir / "projects", backup / "projects")
    return backup


def apply_plan(plan: dict[str, Any]) -> Path:
    data_dir = Path(plan["data_dir"])
    backup = _backup(data_dir)
    blocked = set(plan.get("blocked_project_ids", []))
    safe_projects = [path.name for path in (data_dir / "projects").iterdir()
                     if path.is_dir() and path.name not in blocked]
    for workspace_id in safe_projects:
        root = data_dir / "projects" / workspace_id
        for relative in [*FOLDERS.values(), TRASH_FOLDER]:
            target = root.joinpath(*PurePosixPath(relative).parts)
            target.mkdir(parents=True, exist_ok=True)
    moves: list[FileMove] = [move for move in plan["moves"] if move.workspace_id not in blocked]
    # Copy and hash every target first; the source remains available until all
    # metadata changes commit successfully.
    for move in moves:
        _copy_verified(move)

    database = data_dir / "workbench.db"
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for update in plan["record_updates"]:
            if update["workspace_id"] in blocked:
                continue
            cursor = connection.execute(
                "UPDATE project_artifact SET relative_path=?, metadata_json=? WHERE id=? AND workspace_id=?",
                (update["relative_path"], update["metadata_json"], update["id"], update["workspace_id"]),
            )
            if cursor.rowcount != 1:
                raise MigrationError(f"资料记录已变化：{update['id']}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    # The DB now points only to hash-verified targets. Removing old copies is
    # safe; if interrupted, a rerun recognizes identical targets and finishes.
    for move in moves:
        if move.source.resolve() != move.destination.resolve():
            move.source.unlink(missing_ok=True)
    for workspace_id in safe_projects:
        project_root = data_dir / "projects" / workspace_id
        if not project_root.is_dir() or _is_link(project_root):
            continue
        preserved = {project_root / relative for relative in [*FOLDERS.values(), TRASH_FOLDER]}
        for directory in sorted((p for p in project_root.rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            if directory in preserved:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
    report = {
        "status": "partial" if blocked else "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": plan["data_dir"],
        "backup_dir": str(backup),
        "project_count": plan["project_count"],
        "file_move_count": plan["file_move_count"],
        "record_update_count": plan["record_update_count"],
        "applied_file_move_count": len(moves),
        "applied_record_update_count": sum(
            update["workspace_id"] not in blocked for update in plan["record_updates"]),
        "migrated_project_ids": safe_projects,
        "external_reference_count": plan["external_reference_count"],
        "blocked_project_ids": sorted(blocked),
        "conflicts": plan["conflicts"],
        "sha256_verified": True,
    }
    report_path = backup / "migration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def _validate_dev_path(data_dir: Path, *, local_app_data: str | None = None) -> Path:
    local_app_data = local_app_data or os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        raise MigrationError("无法定位 Run Center 配置")
    config_path = Path(local_app_data) / "GeWuWorkbench" / "RunCenter" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"无法读取 Run Center 配置：{config_path}") from exc
    profiles = config.get("profiles") or {}
    expected = profiles.get("DEV", {}).get("data")
    if not expected:
        raise MigrationError("Run Center 配置没有 DEV 数据路径")
    actual_path = data_dir.resolve(strict=True)
    expected_path = Path(expected).resolve(strict=True)
    if os.path.normcase(str(actual_path)) != os.path.normcase(str(expected_path)):
        raise MigrationError("目标路径与 Run Center 当前 DEV 数据路径不一致")
    for profile_name in ("TEST", "PROD"):
        other = profiles.get(profile_name, {}).get("data")
        if other and os.path.normcase(str(actual_path)) == os.path.normcase(str(Path(other).resolve(strict=True))):
            raise MigrationError(f"拒绝迁移 {profile_name} 数据目录")
    return actual_path


def _require_dev_stopped(*, local_app_data: str | None = None) -> None:
    local_app_data = local_app_data or os.environ.get("LOCALAPPDATA", "")
    run_center = Path(local_app_data) / "GeWuWorkbench" / "RunCenter" / "run-center.ps1"
    if not run_center.is_file():
        raise MigrationError("找不到 Run Center，无法确认 DEV 已停止")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(run_center), "-Profile", "DEV", "-Action", "Status"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise MigrationError("Run Center 无法核验 DEV 停止状态")
    output = result.stdout.casefold()
    page_status = re.search(r"page_status\s*:\s*(\S+)", output)
    api_status = re.search(r"api_status\s*:\s*(\S+)", output)
    if not page_status or not api_status:
        raise MigrationError("Run Center 状态输出不完整，无法确认 DEV 已停止")
    if page_status.group(1) == "listening" or api_status.group(1) == "ok":
        raise MigrationError("DEV 页面或 API 仍在运行；请通过 Run Center 停止 DEV")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path, help="Run Center 配置中的 DEV 数据目录")
    parser.add_argument("--apply", action="store_true", help="备份并执行迁移；默认仅输出预检摘要")
    parser.add_argument("--confirm-dev", action="store_true", help="确认 DEV 已通过 Run Center 停止")
    args = parser.parse_args(argv)
    try:
        data_dir = _validate_dev_path(args.data_dir)
        if args.apply and not args.confirm_dev:
            raise MigrationError("执行迁移需要 --confirm-dev；请先通过 Run Center 停止 DEV")
        if args.apply:
            _require_dev_stopped()
        plan = build_plan(data_dir)
        summary = {key: value for key, value in plan.items() if key not in {"moves", "record_updates"}}
        if args.apply:
            report_path = apply_plan(plan)
            summary.update({"status": "partial" if plan["conflicts"] else "completed",
                            "report": str(report_path)})
        else:
            summary["status"] = "preflight"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if plan["conflicts"] else 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
