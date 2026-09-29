"""Sorftime Streamable HTTP adapter used by the serial Xunlongjue worker."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_URL = re.compile(r'https?://[^\s"\'<>]+', re.I)
_BEARER = re.compile(r'(?i)\bbearer\s+[^\s,;]+')
_KEY = re.compile(r'(?i)(api[_-]?key|secret[_-]?key)([=: ]+)[^&\s,;]+')
_AUDIT_LOCK = threading.Lock()


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


def _redact_audit_value(value: object, key: str = '', field: str = '') -> object:
    """Keep response evidence useful while removing credentials from all shapes."""
    normalized_field = re.sub(r'[^a-z0-9]+', '_', field.lower()).strip('_')
    compact_field = normalized_field.replace('_', '')
    if normalized_field in {
        'key', 'api_key', 'apikey', 'secret', 'secret_key', 'authorization',
        'access_token', 'token', 'password', 'credential', 'credentials',
    } or compact_field in {
        'apikey', 'secretkey', 'accesstoken', 'refreshtoken',
        'authorization', 'password', 'credential', 'credentials',
    } or normalized_field.endswith(('_key', '_token', '_secret', '_password')):
        return '[redacted]'
    if isinstance(value, dict):
        return {str(name): _redact_audit_value(item, key, str(name))
                for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_audit_value(item, key, field) for item in value]
    if isinstance(value, str):
        if key:
            value = value.replace(key, '[密钥已隐藏]')
        value = _BEARER.sub('Bearer [密钥已隐藏]', value)
        value = re.sub(
            r'(?i)([?&](?:api[_-]?key|key|token|access[_-]?token|secret|authorization|password)=)'
            r'[^&#\s"\'<>]+',
            r'\1[密钥已隐藏]', value,
        )
        value = _KEY.sub(r'\1\2[密钥已隐藏]', value)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _record_exchange(name: str, arguments: dict, *, status: str, payload: object,
                     error: str, elapsed_seconds: float, api_key: str,
                     context: dict | None, started_at: str) -> None:
    """Optionally write a task-scoped, credential-redacted Sorftime exchange."""
    raw_dir = os.environ.get('XLJ_SORFTIME_EXCHANGE_DIR', '').strip()
    if not raw_dir:
        return
    directory = Path(raw_dir)
    if not directory.is_absolute():
        raise RuntimeError('Sorftime 交换记录目录必须使用绝对路径')
    if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
        raise RuntimeError('Sorftime 交换记录目录不能是符号链接或 junction')
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
        raise RuntimeError('Sorftime 交换记录目录不能是符号链接或 junction')

    with _AUDIT_LOCK:
        stamp = time.time_ns()
        record = {
            'ordinal': stamp,
            'started_at': started_at,
            'tool': name,
            'request_arguments': _redact_audit_value(arguments, api_key),
            'execution_context': _redact_audit_value(context or {}, api_key),
            'status': status,
            'raw_payload': _redact_audit_value(payload, api_key),
            'error_redacted': scrub(error, api_key) if error else '',
            'duration_seconds': round(max(0.0, float(elapsed_seconds)), 6),
        }
        target = directory / f'exchange-{stamp}-{uuid.uuid4().hex}.json'
        temporary = target.with_name(target.name + f'.{uuid.uuid4().hex}.tmp')
        try:
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False),
                encoding='utf-8',
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


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

    async def call(self, name: str, arguments: dict, *,
                   audit_context: dict | None = None) -> dict:
        if name not in ('category_tree', 'category_trend'):
            raise ValueError('不支持的 Sorftime 工具')
        deadline = None
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        payload = None
        audit_status = 'failed'
        audit_error = ''
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
            audit_status = 'success'
            return payload
        except TimeoutError as exc:
            if deadline is None or not deadline.expired():
                audit_error = scrub(exc, self.api_key)
            else:
                audit_error = f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限'
            raise RuntimeError(audit_error) from None
        except BaseExceptionGroup as exc:
            diagnostic = scrub(exc, self.api_key)
            if deadline is not None and deadline.expired():
                audit_error = (
                    f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限；底层异常：{diagnostic}'
                )
                raise RuntimeError(audit_error) from None
            if isinstance(exc, Exception):
                audit_error = diagnostic
                raise RuntimeError(audit_error) from None
            audit_error = diagnostic
            raise
        except asyncio.CancelledError:
            audit_status = 'cancelled'
            audit_error = 'Sorftime MCP 调用已取消'
            raise
        except Exception as exc:  # noqa: BLE001
            diagnostic = scrub(exc, self.api_key)
            if deadline is not None and deadline.expired():
                audit_error = (
                    f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限；底层异常：{diagnostic}'
                )
                raise RuntimeError(audit_error) from None
            audit_error = diagnostic
            raise RuntimeError(audit_error) from None
        finally:
            _record_exchange(
                name, arguments, status=audit_status, payload=payload,
                error=audit_error, elapsed_seconds=time.monotonic() - started,
                api_key=self.api_key, context=audit_context, started_at=started_at,
            )


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


def call_sync(client: SorftimeClient, name: str, arguments: dict, *,
              audit_context: dict | None = None) -> dict:
    call = client.call
    try:
        parameters = inspect.signature(call).parameters.values()
        supports_context = any(
            parameter.name == 'audit_context' or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_context = True
    if supports_context:
        return asyncio.run(call(name, arguments, audit_context=audit_context))
    return asyncio.run(call(name, arguments))
