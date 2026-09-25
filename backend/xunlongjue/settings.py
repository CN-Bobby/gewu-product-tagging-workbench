"""Private Sorftime MCP settings, stored separately from model credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _root() -> Path:
    from ..db import STORAGE
    return STORAGE / 'xunlongjue'


def _path() -> Path:
    return _root() / 'settings.json'


def _read() -> dict:
    path = _path()
    if not path.is_file():
        return {'endpoint': '', 'api_key': ''}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'endpoint': '', 'api_key': ''}
    return {'endpoint': str(value.get('endpoint') or ''),
            'api_key': str(value.get('api_key') or '')}


def public_endpoint(value: str) -> str:
    """Return only an endpoint origin/path; URL userinfo, query, and fragment are secret-bearing."""
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return ''
        host = parsed.hostname
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path or '/', '', ''))
    except ValueError:
        return ''


def status() -> dict:
    value = _read()
    return {'endpoint': public_endpoint(value['endpoint']),
            'key_configured': bool(value['api_key'])}


def save(endpoint: str, api_key: str) -> dict:
    endpoint = str(endpoint or '').strip()
    api_key = str(api_key or '').strip()
    sanitized = public_endpoint(endpoint)
    if not sanitized:
        raise ValueError('Sorftime MCP 端点必须是有效的 HTTP(S) 地址')
    if not api_key and not _read()['api_key']:
        raise ValueError('请填写 Sorftime API Key')
    if len(endpoint) > 1000 or len(api_key) > 2000 or any(c in api_key for c in '\r\n'):
        raise ValueError('Sorftime 设置格式无效')
    # Never persist URL query/userinfo. The credential has its own field and file.
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read()
    data = {'endpoint': sanitized, 'api_key': api_key or current['api_key']}
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)
    return status()


def clear_key() -> None:
    value = _read()
    value['api_key'] = ''
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def connection() -> tuple[str, str]:
    value = _read()
    if not value['endpoint'] or not value['api_key']:
        raise ValueError('请先配置 Sorftime MCP 端点和 API Key')
    return public_endpoint(value['endpoint']), value['api_key']
