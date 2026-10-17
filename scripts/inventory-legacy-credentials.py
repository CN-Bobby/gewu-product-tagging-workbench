"""List likely legacy credential/config copies without opening or printing secret values.

The inventory is limited to known workbench data/backup roots and the Run Center's old Reasonix
state. It is read-only and writes only a metadata report outside those roots.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tomllib


def has_inline_secret(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).casefold()
            sensitive = ('secret' in name or 'token' in name or 'password' in name
                         or ('api_key' in name and not name.endswith('_env')))
            if sensitive and item not in (None, '', [], {}):
                return True
            if has_inline_secret(item):
                return True
    elif isinstance(value, list):
        return any(has_inline_secret(item) for item in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    local_app = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    roots = [Path(r'K:\Bobbytagging\Data-Test'), Path(r'K:\Bobbytagging\Data'),
             Path(r'K:\Bobbytagging\Backups'), Path(r'K:\GeWuTest\Data'),
             Path(r'K:\GeWuTest\Backups'), Path(r'K:\GeWuTest\Rollback'),
             local_app / 'GeWuWorkbench' / 'RunCenter' / 'reasonix']
    parser.add_argument('--output', type=Path,
                        default=Path(r'K:\GeWuTest\Reports') /
                        f'legacy-credential-inventory-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json')
    args = parser.parse_args()
    findings = {}
    existing_roots = [root.resolve() for root in roots if root.exists()]
    for root in existing_roots:
        for current, dirs, names in os.walk(root, followlinks=False):
            base = Path(current)
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
            for name in names:
                path = base / name
                folded = name.casefold()
                kind = None
                inline_secret = False
                if folded == '.env' or folded.endswith('.env') or folded.startswith('credentials.'):
                    kind = 'credential_file_name_review'
                elif folded == 'settings.json':
                    kind = 'sensitive_settings_file_review'
                elif folded in {'config.toml', 'provider.toml'}:
                    try:
                        inline_secret = has_inline_secret(tomllib.loads(path.read_text(encoding='utf-8-sig')))
                    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                        inline_secret = True
                    if inline_secret:
                        kind = 'inline_credential_field_detected'
                if kind:
                    key = str(path.resolve()).casefold()
                    findings[key] = {'path': str(path.resolve()), 'bytes': path.stat().st_size,
                                     'modified_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                                     'classification': kind, 'inline_credential_field': inline_secret,
                                     'value_scanned_into_report': False}

    report = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'status': 'completed', 'mode': 'read-only-filename-and-config-field-inventory',
              'roots_scanned': [str(root) for root in existing_roots],
              'finding_count': len(findings), 'findings': sorted(findings.values(), key=lambda item: item['path'].casefold()),
              'note': 'Contains paths and file metadata only; no credential values or file hashes. Nothing was deleted or changed.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"inventory: findings={len(findings)}; roots={len(existing_roots)}; report={args.output}")
    print('Metadata only; no secret values were recorded and no files were changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
