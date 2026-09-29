"""Sorftime Streamable HTTP adapter used by the serial Xunlongjue worker."""
from __future__ import annotations

import asyncio
import json
import math
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
    def __init__(self, endpoint: str, api_key: str, *, transport=None,
                 timeout_seconds: float = 180.0) -> None:
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Sorftime MCP 调用超时必须是有限正数')
        self.endpoint = endpoint
        self.api_key = api_key
        self.transport = transport
        self.timeout_seconds = timeout_seconds

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
                return _decode(result, key=self.api_key)

    async def call(self, name: str, arguments: dict) -> dict:
        if name not in ('category_tree', 'category_trend'):
            raise ValueError('不支持的 Sorftime 工具')
        deadline = None
        try:
            if self.transport is None:
                request = self._call_live(name, arguments)
            else:
                request = self.transport(name, arguments)
            # The whole live operation is bounded: connection, MCP initialize,
            # tool call, and transport context cleanup share the baseline limit.
            deadline = asyncio.timeout(self.timeout_seconds)
            async with deadline:
                result = await request
                # Some async clients suppress the cancellation used by
                # asyncio.timeout during transport cleanup. Do not accept a
                # late response merely because that client returned a value.
                if deadline.expired():
                    raise TimeoutError
            payload = result if isinstance(result, dict) else {'data': result}
            if name == 'category_tree' and not _has_tree_items(payload):
                raise ValueError('Sorftime category_tree 返回了无法识别的 JSON 结构')
            return payload
        except TimeoutError as exc:
            if deadline is None or not deadline.expired():
                raise RuntimeError(scrub(exc, self.api_key)) from None
            raise RuntimeError(
                f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限'
            ) from None
        except BaseExceptionGroup as exc:
            diagnostic = scrub(exc, self.api_key)
            if deadline is not None and deadline.expired():
                raise RuntimeError(
                    f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限；底层异常：{diagnostic}'
                ) from None
            if isinstance(exc, Exception):
                raise RuntimeError(diagnostic) from None
            raise
        except Exception as exc:  # noqa: BLE001
            diagnostic = scrub(exc, self.api_key)
            if deadline is not None and deadline.expired():
                raise RuntimeError(
                    f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限；底层异常：{diagnostic}'
                ) from None
            raise RuntimeError(diagnostic) from None


def _result_text(result: Any) -> str:
    return '\n'.join(str(getattr(part, 'text', '')) for part in (getattr(result, 'content', None) or []))


def _decode(result: Any, *, key: str = '') -> dict:
    if getattr(result, 'isError', False):
        raise RuntimeError(scrub(_result_text(result) or 'Sorftime MCP 工具返回错误', key))
    raw = _result_text(result)
    if raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Keep the baseline contract: malformed text is a failure even if
            # the server also supplied structuredContent. Preserve a short,
            # redacted excerpt because Sorftime may return useful plain-text
            # diagnostics such as its own soft-timeout message.
            excerpt = scrub(raw, key)
            if excerpt:
                raise ValueError(f'Sorftime MCP 返回非 JSON 文本：{excerpt}') from None
            raise ValueError('Sorftime MCP 返回非 JSON 文本') from None
    else:
        data = getattr(result, 'structuredContent', None)
        if data is None:
            raise ValueError('Sorftime MCP 返回空文本')
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
