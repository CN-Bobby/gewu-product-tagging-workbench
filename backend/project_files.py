"""Project-scoped artifact registry and local folder layout.

Files are copied into the owning Workspace folder and never moved from a module's
own storage. Existing historical folders can be referenced without relocation.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select, func

from .db import STORAGE, ProjectArtifact, Session, Workspace, now, require

PROJECTS = STORAGE / "projects"
FOLDERS = {
    "source": "source/raw",
    "data_x": "data-x",
    "product_archive": "product-archive",
    "keywords": "keywords",
    "reviews": "reviews",
    "voc": "voc",
    "images": "images",
    "reports": "reports",
    "legacy": "legacy-index",
}
_ID = re.compile(r"^[a-f0-9]{32}$")


def project_dir(workspace_id: str) -> Path:
    if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id):
        raise ValueError("项目编号无效")
    root = PROJECTS.resolve()
    candidate = PROJECTS / workspace_id
    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
        raise ValueError("项目目录不能是链接")
    if candidate.parent.resolve() != root:
        raise ValueError("项目路径越界")
    return candidate


def ensure_project(workspace_id: str) -> Path:
    with Session() as session:
        require(session, Workspace, workspace_id)
    root = project_dir(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    for relative in FOLDERS.values():
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(value: str) -> str:
    name = Path(str(value or "artifact")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "artifact")[:180]


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def register_file(workspace_id: str, kind: str, source_path: str | Path, *,
                  source_type: str, source_id: str, name: str | None = None,
                  metadata: dict[str, Any] | None = None, copy_file: bool = True) -> dict[str, Any]:
    """Register a workflow file, copying it into the project unless explicitly referenced."""
    if kind not in FOLDERS:
        raise ValueError("项目文件类型无效")
    if not source_type or not source_id:
        raise ValueError("项目文件必须带有来源类型和来源编号")
    source = Path(source_path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("项目文件来源不是普通文件")
    digest, size = _hash(source)
    root = ensure_project(workspace_id)
    display_name = _safe_name(name or source.name)
    if copy_file:
        suffix = Path(display_name).suffix[:20]
        stem = Path(display_name).stem[:100]
        target_name = f"{stem}-{digest[:12]}{suffix}"
        target = root / FOLDERS[kind] / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve().is_relative_to(root.resolve()) is False:
            raise ValueError("项目文件路径越界")
        if target.exists():
            existing_digest, _ = _hash(target)
            if existing_digest != digest:
                raise ValueError("项目文件目标已存在但 SHA-256 校验失败")
        else:
            temporary = target.with_name(target.name + ".tmp")
            shutil.copyfile(source, temporary)
            copied_digest, _ = _hash(temporary)
            if copied_digest != digest:
                temporary.unlink(missing_ok=True)
                raise ValueError("项目文件复制后 SHA-256 校验失败")
            os.replace(temporary, target)
        relative_path = target.relative_to(root).as_posix()
    else:
        # A historical reference is indexed in place and is intentionally not downloadable
        # through the project artifact endpoint because it may be outside this project root.
        relative_path = "external:" + str(source)
    with Session.begin() as session:
        require(session, Workspace, workspace_id)
        existing = session.scalar(select(ProjectArtifact).where(
            ProjectArtifact.workspace_id == workspace_id,
            ProjectArtifact.kind == kind,
            ProjectArtifact.source_type == source_type,
            ProjectArtifact.source_id == source_id,
            ProjectArtifact.sha256 == digest,
            ProjectArtifact.relative_path == relative_path,
        ))
        if existing:
            return _public(existing)
        item = ProjectArtifact(workspace_id=workspace_id, kind=kind,
            source_type=str(source_type)[:80], source_id=str(source_id)[:160], name=display_name,
            relative_path=relative_path, sha256=digest, size=size,
            metadata_json=dict(metadata or {}))
        session.add(item)
        session.flush()
        return _public(item)


def register_dataset(workspace_id: str, dataset_id: str, source_path: str | Path,
                     filename: str, sha256: str | None = None) -> dict[str, Any]:
    result = register_file(workspace_id, "source", source_path, source_type="dataset",
        source_id=dataset_id, name=filename, metadata={"dataset_id": dataset_id,
        "source_sha256": sha256} if sha256 else {"dataset_id": dataset_id})
    # Imported images are already content-addressed in the shared asset store. The
    # image artifact references remain recorded as Products/Assets in the database.
    return result


def _public(item: ProjectArtifact) -> dict[str, Any]:
    return {"id": item.id, "workspace_id": item.workspace_id, "kind": item.kind,
            "source_type": item.source_type, "source_id": item.source_id, "name": item.name,
            "relative_path": item.relative_path, "sha256": item.sha256, "size": item.size,
            "metadata": item.metadata_json or {}, "created_at": item.created_at,
            "downloadable": not str(item.relative_path).startswith("external:")}


def list_artifacts(workspace_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    with Session() as session:
        require(session, Workspace, workspace_id)
        query = select(ProjectArtifact).where(ProjectArtifact.workspace_id == workspace_id)
        if kind:
            if kind not in FOLDERS:
                raise ValueError("项目文件类型无效")
            query = query.where(ProjectArtifact.kind == kind)
        rows = session.scalars(query.order_by(ProjectArtifact.created_at.desc(), ProjectArtifact.id.desc())).all()
        return [_public(row) for row in rows]


def artifact_path(workspace_id: str, artifact_id: str) -> Path:
    root = project_dir(workspace_id).resolve()
    with Session() as session:
        item = session.get(ProjectArtifact, artifact_id)
        if not item or item.workspace_id != workspace_id:
            raise ValueError("项目文件不存在")
        if item.relative_path.startswith("external:"):
            raise ValueError("历史文件仅登记索引，不通过项目页面下载")
        candidate = (root / Path(item.relative_path)).resolve(strict=True)
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("项目文件路径无效")
    digest, _ = _hash(candidate)
    if digest != item.sha256:
        raise ValueError("项目文件校验失败")
    return candidate


def public_project(workspace_id: str) -> dict[str, Any]:
    root = project_dir(workspace_id)
    with Session() as session:
        workspace = require(session, Workspace, workspace_id)
        count = session.scalar(select(func.count()).select_from(ProjectArtifact).where(
            ProjectArtifact.workspace_id == workspace_id)) or 0
        return {"id": workspace.id, "name": workspace.name, "created_at": workspace.created_at,
                "root": str(root), "artifact_count": int(count or 0)}
