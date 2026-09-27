"""Sorftime Streamable HTTP adapter used by the serial Xunlongjue worker."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_URL = re.compile(r'https?://[^\s"\'<>]+', re.I)
_BEARER = re.compile(r'(?i)\bbearer\s+[^\s,;]+')
_KEY = re.compile(r'(?i)(api[_-]?key|secret[_-]?key)([=: ]+)[^&\s,;]+')


def scrub(text: object, key: str = '') -> str:
    value = _diagnostic_text(text)
    if key:
        value = value.replace(key, '[密钥已隐藏]')
    value = _BEARER.sub('Bearer [密钥已隐藏]', value)
    value = _KEY.sub(r'\1\2[密钥已隐藏]', value)
    value = _URL.sub('[服务地址已隐藏]', value)
    return value.replace('\r', ' ').replace('\n', ' ')[:500]


def _diagnostic_text(value: object) -> str:
    """Keep useful leaf causes when asyncio reports an ExceptionGroup."""
    if isinstance(value, BaseExceptionGroup):
        return '; '.join(_diagnostic_text(child) for child in value.exceptions)
    if isinstance(value, BaseException):
        return f'{type(value).__name__}: {value}'
    return str(value or '')


class SorftimeClient:
    def __init__(self, endpoint: str, api_key: str, *, transport=None) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.transport = transport

    async def _call_live(self, name: str, arguments: dict) -> dict:
        # Sorftime's documented MCP transport authenticates with ?key=. Keep the
        # key out of snapshots, arguments, and diagnostics.
        separator = '&' if '?' in self.endpoint else '?'
        auth_endpoint = self.endpoint + separator + 'key=' + quote(self.api_key, safe='')
        async with streamable_http_client(
            auth_endpoint, terminate_on_close=False,
        ) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments)
                if getattr(result, 'isError', False):
                    raise RuntimeError(scrub(_result_text(result), self.api_key))
                return _decode(result)

    async def call(self, name: str, arguments: dict) -> dict:
        if name not in ('category_tree', 'category_trend'):
            raise ValueError('不支持的 Sorftime 工具')
        try:
            if self.transport is None:
                result = await self._call_live(name, arguments)
            else:
                result = await self.transport(name, arguments)
            payload = result if isinstance(result, dict) else {'data': result}
            if name == 'category_tree' and not _has_tree_items(payload):
                raise ValueError('Sorftime category_tree 返回了无法识别的 JSON 结构')
            return payload
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(scrub(exc, self.api_key)) from None


def _result_text(result: Any) -> str:
    return '\n'.join(str(getattr(part, 'text', '')) for part in (getattr(result, 'content', None) or []))


def _decode(result: Any) -> dict:
    raw = _result_text(result)
    if not raw.strip():
        raise ValueError('Sorftime MCP 返回空文本')
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError('Sorftime MCP 返回非 JSON 文本') from None
    return data if isinstance(data, dict) else {'data': data}


def _has_tree_items(payload: object) -> bool:
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    return any(isinstance(payload.get(name), list)
               for name in ('items', 'subcategories', 'data'))


def call_sync(client: SorftimeClient, name: str, arguments: dict) -> dict:
    return asyncio.run(client.call(name, arguments))
