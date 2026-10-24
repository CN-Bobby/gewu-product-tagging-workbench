"""Project-scoped artifact registry and local folder layout.

Files are copied into the owning Workspace folder and never moved from a module's
own storage. Existing historical folders can be referenced without relocation.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, func

from .db import STORAGE, ProjectArtifact, Session, Workspace, now, require

PROJECTS = STORAGE / "projects"
FOLDERS = {
    "source": "source",
    "data_x": "data-x",
    # Module artifacts stay in the project storage root but outside the six visible
    # user categories. The project tree intentionally presents only the first six.
    "product_archive": "_system/product-archive",
    "reviews": "reviews",
    "voc": "voc",
    "keywords": "keywords",
    "other": "other",
    "images": "_system/images",
    "reports": "_system/reports",
    "legacy": "_system/legacy-index",
}
TRASH_FOLDER = "_system/trash"
MANAGED_EXTENSIONS = {".xlsx", ".html"}
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
    create_project_directories(workspace_id)
    return root


def create_project_directories(workspace_id: str) -> Path:
    """Create the canonical user categories and reserved system directories."""
    root = project_dir(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    for relative in [*FOLDERS.values(), TRASH_FOLDER]:
        target = root / relative
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ValueError("项目类别目录不能是链接")
        target.mkdir(parents=True, exist_ok=True)
    return root


def remove_empty_project_directories(workspace_id: str) -> None:
    """Best-effort rollback for project creation; never removes files."""
    root = project_dir(workspace_id)
    if not root.exists() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        return
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()),
                            key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _safe_name(value: str) -> str:
    name = Path(str(value or "artifact")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "artifact")[:180]


def _upload_name(value: str) -> str:
    """Uploads accept a leaf filename and the two project-library formats only."""
    if not isinstance(value, str) or not value.strip() or Path(value).name != value:
        raise ValueError("文件名无效，不能包含路径")
    name = _safe_name(value)
    if Path(name).suffix.casefold() not in MANAGED_EXTENSIONS:
        raise ValueError("仅支持 .xlsx 和 .html 文件")
    return name


def _install_no_overwrite(target: Path, content: bytes) -> None:
    """Atomically install bytes while failing if another artifact owns the name."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".project-file-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            reservation = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError("目标文件已存在，未覆盖") from exc
        os.close(reservation)
        try:
            os.replace(temporary, target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _managed_path(workspace_id: str, relative_path: str) -> tuple[Path, Path]:
    """Resolve a stored relative path and reject external/symlink references."""
    if not relative_path or relative_path.startswith("external:"):
        raise ValueError("历史外部引用不能移动或回收")
    root = project_dir(workspace_id).resolve()
    candidate = (root / relative_path).resolve(strict=False)
    if not candidate.is_relative_to(root) or candidate == root:
        raise ValueError("项目文件路径越界")
    return root, candidate


def _category_target(root: Path, kind: str, name: str) -> Path:
    category = root
    for part in Path(FOLDERS[kind]).parts:
        category = category / part
        if category.is_symlink() or getattr(category, "is_junction", lambda: False)():
            raise ValueError("项目类别目录不能是链接")
    target = category / name
    resolved_root = root.resolve()
    if not target.resolve(strict=False).is_relative_to(resolved_root):
        raise ValueError("项目文件路径越界")
    return target


def _copy_no_overwrite(source: Path, destination: Path) -> None:
    """Copy through a same-directory temp file, reserving destination first."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".project-move-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as original:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            reservation = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError("目标文件已存在，未覆盖") from exc
        os.close(reservation)
        try:
            os.replace(temporary, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _unlink_if_unreferenced(workspace_id: str, relative_path: str) -> bool:
    """Remove a managed copy only after its last active artifact reference moves."""
    with Session() as session:
        references = session.scalars(select(ProjectArtifact).where(
            ProjectArtifact.workspace_id == workspace_id,
            ProjectArtifact.relative_path == relative_path,
        )).all()
        if any(not (reference.metadata_json or {}).get("deleted_at") for reference in references):
            return False
    _, path = _managed_path(workspace_id, relative_path)
    path.unlink(missing_ok=True)
    return True


def upload(workspace_id: str, name: str, content: bytes, kind: str = "other", *,
           source_type: str = "manual_upload", source_id: str | None = None) -> dict[str, Any]:
    """Register an uploaded XLSX/HTML as a managed, project-owned artifact."""
    filename = _upload_name(name)
    if kind not in FOLDERS:
        raise ValueError("项目文件类型无效")
    if not isinstance(content, bytes) or not content:
        raise ValueError("文件内容为空或无效")
    if len(content) > 100 * 1024 * 1024:
        raise ValueError("文件超过 100 MB")
    root = ensure_project(workspace_id)
    target = _category_target(root, kind, filename)
    digest = hashlib.sha256(content).hexdigest()
    _install_no_overwrite(target, content)
    try:
        with Session.begin() as session:
            require(session, Workspace, workspace_id)
            item = ProjectArtifact(workspace_id=workspace_id, kind=kind,
                source_type=source_type[:80], source_id=(source_id or os.urandom(16).hex())[:160],
                name=filename, relative_path=target.relative_to(root).as_posix(),
                sha256=digest, size=len(content), metadata_json={"manually_uploaded": True})
            session.add(item)
            session.flush()
            result = _public(item)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return result


def update(workspace_id: str, artifact_id: str, *, name: str | None = None,
           kind: str | None = None, target_workspace_id: str | None = None) -> dict[str, Any]:
    """Rename or move a managed copy, preserving its source identity and hash."""
    destination_workspace = target_workspace_id or workspace_id
    with Session() as session:
        item = session.get(ProjectArtifact, artifact_id)
        if not item or item.workspace_id != workspace_id:
            raise ValueError("项目文件不存在或不属于当前项目")
        if (item.metadata_json or {}).get("deleted_at"):
            raise ValueError("回收站中的文件不能直接编辑")
        old = _public(item)
    if not old["downloadable"]:
        raise ValueError("历史外部引用不能移动或重命名")
    new_name = _upload_name(name) if name is not None else old["name"]
    new_kind = kind or old["kind"]
    if new_kind not in FOLDERS:
        raise ValueError("项目文件类型无效")
    old_root, source = _managed_path(workspace_id, old["relative_path"])
    if not source.is_file():
        raise ValueError("托管文件缺失，无法移动")
    destination_root = ensure_project(destination_workspace)
    destination = _category_target(destination_root, new_kind, new_name)
    if source.resolve() == destination.resolve():
        return old
    _copy_no_overwrite(source, destination)
    relative = destination.relative_to(destination_root).as_posix()
    try:
        with Session.begin() as session:
            item = session.get(ProjectArtifact, artifact_id)
            if not item or item.workspace_id != workspace_id:
                raise ValueError("项目文件不存在或项目归属已变化")
            item.workspace_id = destination_workspace
            item.kind = new_kind
            item.name = new_name
            item.relative_path = relative
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    _unlink_if_unreferenced(workspace_id, old["relative_path"])
    with Session() as session:
        return _public(session.get(ProjectArtifact, artifact_id))


def recycle(workspace_id: str, artifact_id: str) -> dict[str, Any]:
    """Move the managed copy to per-project trash and retain its database record."""
    with Session() as session:
        item = session.get(ProjectArtifact, artifact_id)
        if not item or item.workspace_id != workspace_id:
            raise ValueError("项目文件不存在或不属于当前项目")
        metadata = dict(item.metadata_json or {})
        if metadata.get("deleted_at"):
            return _public(item)
        public = _public(item)
    if not public["downloadable"]:
        raise ValueError("历史外部引用不能回收")
    root, source = _managed_path(workspace_id, public["relative_path"])
    if not source.is_file():
        raise ValueError("托管文件缺失，无法回收")
    trash_relative = f"{TRASH_FOLDER}/{artifact_id}-{source.name}"
    _, target = _managed_path(workspace_id, trash_relative)
    _copy_no_overwrite(source, target)
    stamp = datetime.now(timezone.utc).isoformat()
    metadata.update({"deleted_at": stamp, "restore_path": public["relative_path"]})
    try:
        with Session.begin() as session:
            item = session.get(ProjectArtifact, artifact_id)
            if not item or item.workspace_id != workspace_id:
                raise ValueError("项目文件不存在或项目归属已变化")
            item.relative_path = trash_relative
            item.metadata_json = metadata
    except Exception:
        target.unlink(missing_ok=True)
        raise
    _unlink_if_unreferenced(workspace_id, public["relative_path"])
    with Session() as session:
        return _public(session.get(ProjectArtifact, artifact_id))


def restore(workspace_id: str, artifact_id: str) -> dict[str, Any]:
    with Session() as session:
        item = session.get(ProjectArtifact, artifact_id)
        if not item or item.workspace_id != workspace_id:
            raise ValueError("项目文件不存在或不属于当前项目")
        metadata = dict(item.metadata_json or {})
        if not metadata.get("deleted_at"):
            return _public(item)
        public = _public(item)
    root, source = _managed_path(workspace_id, public["relative_path"])
    restore_path = metadata.get("restore_path") or source.name
    destination_relative = f"{FOLDERS[public['kind']]}/{Path(str(restore_path).replace(chr(92), '/')).name}"
    _, destination = _managed_path(workspace_id, destination_relative)
    if not source.is_file():
        raise ValueError("回收站文件缺失，无法恢复")
    _copy_no_overwrite(source, destination)
    metadata.update({"deleted_at": None, "restore_path": None})
    try:
        with Session.begin() as session:
            item = session.get(ProjectArtifact, artifact_id)
            if not item or item.workspace_id != workspace_id:
                raise ValueError("项目文件不存在或项目归属已变化")
            item.relative_path = destination_relative
            item.metadata_json = metadata
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    source.unlink(missing_ok=True)
    with Session() as session:
        return _public(session.get(ProjectArtifact, artifact_id))


def list_trash(workspace_id: str) -> list[dict[str, Any]]:
    with Session() as session:
        require(session, Workspace, workspace_id)
        rows = session.scalars(select(ProjectArtifact).where(
            ProjectArtifact.workspace_id == workspace_id).order_by(ProjectArtifact.created_at.desc())).all()
        return [_public(row) for row in rows if (row.metadata_json or {}).get("deleted_at")]


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
        target = _category_target(root, kind, target_name)
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
    metadata = item.metadata_json or {}
    source_identity = f"{item.source_type}\0{item.source_id}".encode("utf-8")
    return {"id": item.id, "workspace_id": item.workspace_id, "kind": item.kind,
            "source_type": item.source_type, "source_id": item.source_id, "name": item.name,
            "source_identity": f"{item.source_type}:{item.source_id}",
            "relative_path": item.relative_path, "sha256": item.sha256, "size": item.size,
            "metadata": metadata, "created_at": item.created_at,
            "version_group": hashlib.sha256(source_identity).hexdigest()[:24],
            "deleted_at": metadata.get("deleted_at"),
            "downloadable": not str(item.relative_path).startswith("external:") and not metadata.get("deleted_at")}


def list_artifacts(workspace_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    with Session() as session:
        require(session, Workspace, workspace_id)
        query = select(ProjectArtifact).where(ProjectArtifact.workspace_id == workspace_id)
        if kind:
            if kind not in FOLDERS:
                raise ValueError("项目文件类型无效")
            query = query.where(ProjectArtifact.kind == kind)
        rows = session.scalars(query.order_by(ProjectArtifact.created_at.desc(), ProjectArtifact.id.desc())).all()
        return [_public(row) for row in rows if not (row.metadata_json or {}).get("deleted_at")]


def artifact_path(workspace_id: str, artifact_id: str) -> Path:
    root = project_dir(workspace_id).resolve()
    with Session() as session:
        item = session.get(ProjectArtifact, artifact_id)
        if not item or item.workspace_id != workspace_id:
            raise ValueError("项目文件不存在")
        if (item.metadata_json or {}).get("deleted_at"):
            raise ValueError("回收站中的文件不可下载")
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
    project_dir(workspace_id)
    with Session() as session:
        workspace = require(session, Workspace, workspace_id)
        count = session.scalar(select(func.count()).select_from(ProjectArtifact).where(
            ProjectArtifact.workspace_id == workspace_id)) or 0
        return {"id": workspace.id, "name": workspace.name, "created_at": workspace.created_at,
                "artifact_count": int(count or 0)}
