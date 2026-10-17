"""Copy non-secret provider/model choices into the persistent DEV and TEST profiles.

The source and destination TOML files are never printed. Preview is the default; ``--apply`` only
creates missing files and refuses to replace an existing profile configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tomllib


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def reject_inline_secrets(value, path='') -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).casefold()
            location = f'{path}.{key}' if path else str(key)
            if any(token in name for token in ('secret', 'token', 'password')) or (
                    'api_key' in name and not name.endswith('_env')):
                if item not in (None, '', [], {}):
                    raise ValueError(f'Provider config contains an inline credential field at {location}')
            reject_inline_secrets(item, location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_inline_secrets(item, f'{path}[{index}]')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-storage', type=Path,
                        default=Path(r'K:\Bobbytagging\Data-Test'))
    parser.add_argument('--install-root', type=Path,
                        default=Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) /
                        'GeWuWorkbench' / 'RunCenter')
    parser.add_argument('--apply', action='store_true', help='Create missing DEV/TEST config.toml copies')
    args = parser.parse_args()
    source = args.source_storage / 'providers' / 'config.toml'
    if not source.is_file():
        parser.error('Source provider config.toml is missing')
    parsed = tomllib.loads(source.read_text(encoding='utf-8-sig'))
    reject_inline_secrets(parsed)
    source_hash = sha256(source)
    destinations = {
        name: args.install_root / 'profiles' / name / 'providers' / 'config.toml'
        for name in ('DEV', 'TEST')
    }
    for name, target in destinations.items():
        if target.exists() and sha256(target) != source_hash:
            parser.error(f'{name} provider config already exists with different content; it was not changed')

    mode = 'apply' if args.apply else 'preview'
    for name, target in destinations.items():
        state = 'already-identical' if target.exists() else ('created' if args.apply else 'would-create')
        if args.apply and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target) != source_hash:
                target.unlink(missing_ok=True)
                parser.error(f'{name} config copy failed SHA-256 verification')
        print(f'{mode}: profile={name}; providers={len(parsed.get("providers", []))}; '
              f'models={sum(len(p.get("models", [])) for p in parsed.get("providers", []) if isinstance(p, dict))}; '
              f'state={state}; path={target}; sha256={source_hash}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
