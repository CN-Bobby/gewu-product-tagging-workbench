"""Remove legacy credential files from a backup created by backup-production-data.py.

This only edits a verified backup directly under K:\\Bobbytagging\\Backups. The live PROD data is
never changed. Files removed from the backup remain intact in their original source locations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


SOURCE = Path(r'K:\Bobbytagging\Data').resolve()
BACKUP_ROOT = Path(r'K:\Bobbytagging\Backups').resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def is_sensitive(relative: str) -> bool:
    path = Path(relative)
    return (path.name.casefold() in {'.env', 'credentials.env', 'settings.json'}
            or path.suffix.casefold() == '.log'
            or any(part.casefold() == 'logs' for part in path.parts))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args(argv)
    backup = args.backup.resolve()
    if backup.parent != BACKUP_ROOT or not backup.is_dir():
        parser.error('Backup must be an existing direct child of K:\\Bobbytagging\\Backups')
    manifest_path = backup / 'backup-manifest.json'
    if not manifest_path.is_file():
        parser.error('Backup manifest is missing; no files were changed')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if Path(manifest.get('source', '')).resolve() != SOURCE or manifest.get('status') != 'verified':
        parser.error('Backup manifest is not a verified snapshot of the exact PROD data directory')
    rows = manifest.get('files')
    if not isinstance(rows, list):
        parser.error('Backup inventory is invalid; no files were changed')
    remove = [row for row in rows if isinstance(row, dict) and is_sensitive(str(row.get('path', '')))]
    keep = [row for row in rows if row not in remove]
    for row in remove:
        relative = Path(str(row['path']))
        if relative.is_absolute() or '..' in relative.parts:
            parser.error('Backup manifest contains an unsafe relative path; no files were changed')
        path = (backup / relative).resolve()
        if backup not in path.parents or not path.is_file():
            parser.error('A credential-file backup path is missing or escapes the backup directory')
    print(f'backup={backup}; verified_files={len(rows)}; credential_or_log_files_to_remove={len(remove)}')
    print('Source data and business files remain untouched.')

    for row in remove:
        (backup / Path(str(row['path']))).unlink()
    for row in keep:
        path = (backup / Path(str(row['path']))).resolve()
        if not path.is_file() or sha256(path) != str(row.get('sha256', '')).lower():
            raise RuntimeError('A retained backup file failed its recorded SHA-256 check')
    manifest['files'] = keep
    manifest['file_count'] = len(keep)
    manifest['excluded_sensitive_or_logs_count'] = int(manifest.get('excluded_sensitive_or_logs_count', 0)) + len(remove)
    manifest['sensitive_copy_cleanup'] = {
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'removed_file_count': len(remove),
        'source_unchanged': True,
        'retained_files_reverified': True,
    }
    manifest['note'] = 'Credential files and logs are excluded. SQLite uses online backup snapshots; all retained files were SHA-256 verified.'
    temp = manifest_path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(manifest_path)
    print(f"sanitized backup verified: retained={len(keep)}; removed={len(remove)}; "
          f"manifest_sha256={sha256(manifest_path)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
