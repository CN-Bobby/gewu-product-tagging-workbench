"""One-time, non-destructive import of credentials into the Windows DPAPI vault.

Preview by default. Pass ``--apply`` to store values. Source files are never modified or removed;
the command reports counts and presence only and never prints credential content.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from backend.credential_vault import get_secret, set_secret, profile_scope


def _providers(source: Path) -> list[dict]:
    path = source / 'providers' / 'config.toml'
    if not path.is_file():
        return []
    import tomllib
    return tomllib.loads(path.read_text(encoding='utf-8-sig')).get('providers', [])


def collect(source: Path) -> list[tuple[str, str, str]]:
    """Return (namespace, identifier, secret) entries; callers must not display values."""
    entries: list[tuple[str, str, str]] = []
    provider_path = source / 'providers' / '.env'
    env = dotenv_values(provider_path) if provider_path.is_file() else {}
    names = set()
    for provider in _providers(source):
        if not isinstance(provider, dict):
            continue
        for credential in provider.get('credentials') or []:
            if isinstance(credential, dict) and credential.get('api_key_env'):
                names.add(str(credential['api_key_env']))
        if provider.get('api_key_env'):
            names.add(str(provider['api_key_env']))
    for name in sorted(names):
        value = env.get(name)
        if isinstance(value, str) and value:
            entries.append(('model', name, value))

    sellersprite = source / 'keyword-research' / 'credentials.env'
    if sellersprite.is_file():
        values = dotenv_values(sellersprite)
        secret = values.get('SELLERSPRITE_MCP_SECRET_KEY')
        if isinstance(secret, str) and secret:
            entries.append(('sellersprite', 'mcp_secret_key', secret))

    return entries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-storage', required=True, type=Path, help='数据目录，读取 providers/、keyword-research/、xunlongjue/')
    parser.add_argument('--profile', choices=('dev', 'test', 'prod'), default='dev')
    parser.add_argument('--apply', action='store_true', help='确认写入当前用户 DPAPI 凭证库；源文件保留不变')
    args = parser.parse_args(argv)
    source = args.source_storage.expanduser().resolve()
    if not source.is_dir():
        parser.error('源数据目录不存在')
    entries = collect(source)
    scope = profile_scope(args.profile)
    imported = skipped = 0
    for namespace, key, secret in entries:
        if get_secret(namespace, key, args.profile):
            skipped += 1
        elif args.apply:
            set_secret(namespace, key, secret, args.profile)
            imported += 1
    mode = '已写入' if args.apply else '预览'
    print(f'{mode}完成：环境={scope}，发现凭证={len(entries)}，新增={imported}，已存在跳过={skipped}。源文件未修改。')
    if not args.apply:
        print('确认迁移后加 --apply；不会显示密钥内容。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
