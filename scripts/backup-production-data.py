"""Create a verified, current-user-only snapshot of the existing PROD data directory.

SQLite databases are snapshotted through SQLite's online backup API. The source is never modified.
Preview is read-only; --apply creates a new destination and never overwrites an existing backup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


SOURCE = Path(r'K:\Bobbytagging\Data')
BACKUP_ROOT = Path(r'K:\Bobbytagging\Backups')
EXCLUDED_SUFFIXES = ('-wal', '-shm')
SQLITE_SUFFIXES = {'.db', '.sqlite', '.sqlite3'}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def files_under(root: Path):
    result = []
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        if any((base / directory).is_symlink() for directory in dirs):
            raise ValueError(f'Symbolic directory link needs separate review: {base.relative_to(root)}')
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            path = base / name
            if path.is_symlink():
                raise ValueError(f'Symbolic link needs separate review: {path.relative_to(root)}')
            result.append(path)
    return result


def is_sidecar(path: Path) -> bool:
    name = path.name.casefold()
    return name == 'runtime.lock' or any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def is_sensitive_copy(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    name = path.name.casefold()
    return (name in {'.env', 'credentials.env', 'settings.json'} or path.suffix.casefold() == '.log'
            or any(part.casefold() == 'logs' for part in relative.parts))


def database_backup(source: Path, target: Path) -> None:
    uri = source.resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=15) as origin, sqlite3.connect(target) as copy:
        origin.backup(copy, pages=256, sleep=0.05)
        result = copy.execute('PRAGMA quick_check').fetchone()
        if not result or result[0] != 'ok':
            raise RuntimeError('SQLite quick_check failed')
    shutil.copystat(source, target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--target', type=Path, help='New directory under K:\\Bobbytagging\\Backups')
    parser.add_argument('--apply', action='store_true', help='Create the backup after previewing its scope')
    args = parser.parse_args(argv)
    source = args.source.resolve()
    backup_root = BACKUP_ROOT.resolve()
    if str(source).casefold() != str(SOURCE.resolve()).casefold():
        parser.error('This guarded migration backup accepts only K:\\Bobbytagging\\Data')
    if not source.is_dir():
        parser.error('PROD data directory is missing')
    files = files_under(source)
    included = [p for p in files if not is_sidecar(p)]
    excluded = [p.relative_to(source).as_posix() for p in files if is_sidecar(p)]
    sensitive = [p.relative_to(source).as_posix() for p in included if is_sensitive_copy(p, source)]
    included = [p for p in included if not is_sensitive_copy(p, source)]
    total_bytes = sum(p.stat().st_size for p in included)
    sqlite_count = sum(p.suffix.casefold() in SQLITE_SUFFIXES for p in included)
    target = (args.target or backup_root / ('pre-run-center-' + datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
    if target == source or source in target.parents or target in source.parents:
        parser.error('Backup target overlaps the source data directory')
    if target.parent != backup_root:
        parser.error('Backup target must be a direct child of K:\\Bobbytagging\\Backups')
    if target.exists():
        parser.error('Backup destination already exists and will not be overwritten')
    print(f"scope: {source}; files={len(included)}; bytes={total_bytes}; sqlite_databases={sqlite_count}; "
          f"excluded_live_sidecars={len(excluded)}; excluded_sensitive_or_logs={len(sensitive)}")
    print(f'target: {target}')
    if not args.apply:
        print('Preview only. Add --apply to create the backup and verify every copied file.')
        return 0

    backup_root.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=False)
    identity = subprocess.check_output(['whoami'], text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()
    acl = subprocess.run(['icacls', str(target), '/inheritance:r', '/grant:r', f'{identity}:(OI)(CI)F'],
                         capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if acl.returncode:
        raise RuntimeError('Could not restrict backup directory to the current Windows user; backup content was not copied')

    rows = []
    report = target / 'backup-manifest.json'
    try:
        for path in included:
            relative = path.relative_to(source)
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            method = 'sqlite-online-backup' if path.suffix.casefold() in SQLITE_SUFFIXES else 'copy-and-sha256'
            if method == 'sqlite-online-backup':
                database_backup(path, output)
            else:
                shutil.copy2(path, output)
            digest = sha256(output)
            if method == 'copy-and-sha256' and sha256(path) != digest:
                raise RuntimeError(f'File changed during copy: {relative.as_posix()}')
            rows.append({'path': relative.as_posix(), 'bytes': output.stat().st_size,
                         'sha256': digest, 'method': method})
        result = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'status': 'verified', 'source': str(source), 'target': str(target),
                  'file_count': len(rows), 'sqlite_databases': sqlite_count,
                  'excluded_runtime_files': excluded, 'excluded_sensitive_or_logs_count': len(sensitive),
                  'excluded_sensitive_or_logs': sensitive, 'files': rows,
                  'note': 'Credential files and logs are excluded. SQLite uses online backup snapshots; all other files were SHA-256 verified.'}
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        result = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'status': 'partial_failed', 'source': str(source), 'target': str(target),
                  'copied_file_count': len(rows), 'error_type': type(exc).__name__,
                  'files': rows, 'excluded_runtime_files': excluded,
                  'excluded_sensitive_or_logs_count': len(sensitive)}
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    print(f"verified backup: {target}; files={len(rows)}; manifest={report}; sha256={sha256(report)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
