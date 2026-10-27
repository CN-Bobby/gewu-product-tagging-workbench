"""Rebind a copied DEV demo snapshot to its new, sibling Data directory.

This helper ships beside the frozen portable app. It never reads credential vaults,
and it refuses a data directory without the snapshot marker and matching app.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

BUILD_ID = "0.5.0-beta.15+ddc2b996e650"
COMMIT = "ddc2b996e650b96b7bbbe69b57085fa18d116d38"
DATABASES = ("workbench.db", "amazon-voc/voc.sqlite3")
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".toml", ".md"}


def _replace(value: str, old: str, new: str) -> str:
    # JSON strings hold backslashes twice; ordinary database paths hold them once.
    return value.replace(old.replace("\\", "\\\\"), new.replace("\\", "\\\\")).replace(old, new)


def _database_paths(database: Path, old: str, new: str, backup: Path) -> int:
    if not database.is_file():
        return 0
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, closing(sqlite3.connect(backup)) as saved:
        connection.backup(saved)
        connection.execute("BEGIN IMMEDIATE")
        changed = 0
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            quoted_table = '"' + table.replace('"', '""') + '"'
            for column in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall():
                name, kind = column[1], column[2].upper()
                if kind not in {"TEXT", "VARCHAR", "JSON", ""}:
                    continue
                quoted_column = '"' + name.replace('"', '""') + '"'
                rows = connection.execute(
                    f"SELECT rowid, {quoted_column} FROM {quoted_table} WHERE {quoted_column} IS NOT NULL"
                ).fetchall()
                for rowid, value in rows:
                    if not isinstance(value, str) or (old not in value and old.replace("\\", "\\\\") not in value):
                        continue
                    updated = _replace(value, old, new)
                    if updated != value:
                        connection.execute(
                            f"UPDATE {quoted_table} SET {quoted_column}=? WHERE rowid=?",
                            (updated, rowid),
                        )
                        changed += 1
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return changed


def relocate(data: Path) -> dict[str, int]:
    data = data.resolve()
    marker = data / ".demo-source-root"
    app = data.parent / "app"
    if not marker.is_file() or not (app / "Workbench.exe").is_file():
        raise ValueError("只接受演示套件中的 Data 目录；缺少快照标记或程序。")
    manifest = json.loads((app / "build-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("build_id") != BUILD_ID or manifest.get("commit") != COMMIT or manifest.get("dirty"):
        raise ValueError("演示数据与程序版本不匹配，拒绝迁移。")
    old = marker.read_text(encoding="utf-8").strip()
    if not old or not Path(old).is_absolute():
        raise ValueError("快照来源路径无效，拒绝迁移。")
    new = str(data)
    if os.path.normcase(os.path.normpath(old)) == os.path.normcase(os.path.normpath(new)):
        return {"database_fields": 0, "text_files": 0}

    sys.path.insert(0, str(app))
    from backend.storage_lock import exclusive_storage

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = data / ".demo-relocation-backups" / timestamp
    database_fields = text_files = 0
    with exclusive_storage(data):
        for relative in DATABASES:
            database_fields += _database_paths(data / relative, old, new, backup_root / relative)
        for path in data.rglob("*"):
            if (not path.is_file() or path == marker or path.suffix.lower() not in TEXT_SUFFIXES
                    or ".demo-relocation-backups" in path.parts or "providers" in path.parts):
                continue
            value = path.read_text(encoding="utf-8")
            updated = _replace(value, old, new)
            if updated == value:
                continue
            relative = path.relative_to(data)
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            temporary = path.with_name(path.name + ".relocating")
            temporary.write_text(updated, encoding="utf-8")
            os.replace(temporary, path)
            text_files += 1
        marker.write_text(new, encoding="utf-8")
    return {"database_fields": database_fields, "text_files": text_files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args()
    result = relocate(args.data_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
