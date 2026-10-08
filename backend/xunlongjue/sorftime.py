"""Sorftime Streamable HTTP adapter used by the serial Xunlongjue worker."""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
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
_HEADER_SECRET = re.compile(
    r'(?im)(^\s*(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*)[^\r\n]+'
)
_AUDIT_LOCK = threading.Lock()
_LIVE_RESPONSE_AUDIT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    'xunlongjue_live_response_audit', default=None,
)


class SorftimeDecodeError(ValueError):
    """A Sorftime result could not be converted to the engine's wire contract."""

    def __init__(self, message: str, raw_text: str, *, stage: str,
                 response_metadata: dict | None = None,
                 wire_response: dict | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.stage = stage
        self.response_metadata = response_metadata or {}
        self.wire_response = wire_response or {}


@dataclass
class _LiveCallResult:
    result: Any
    wire_response: dict


def scrub(text: object, key: str = '') -> str:
    value = _diagnostic_text(text)
    if key:
        value = value.replace(key, '[密钥已隐藏]')
    value = _HEADER_SECRET.sub(r'\1[密钥已隐藏]', value)
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
    sensitive_field = normalized_field in {
        'key', 'api_key', 'apikey', 'secret', 'secret_key', 'authorization',
        'access_token', 'token', 'password', 'credential', 'credentials',
        'cookie', 'set_cookie', 'proxy_authorization',
    } or compact_field in {
        'apikey', 'secretkey', 'accesstoken', 'refreshtoken',
        'authorization', 'password', 'credential', 'credentials', 'setcookie',
    } or normalized_field.endswith(('_key', '_token', '_secret', '_password', '_cookie', '_cookies')) \
        or any(marker in compact_field for marker in (
            'apikey', 'secret', 'token', 'password', 'credential', 'authorization', 'cookie',
        ))
    if sensitive_field and not (
        normalized_field == 'key' and isinstance(value, str)
        and re.fullmatch(r'\d{4}[-/.]\d{1,2}', value.strip())
    ):
        return '[redacted]'
    if isinstance(value, dict):
        return {str(name): _redact_audit_value(item, key, str(name))
                for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_audit_value(item, key, field) for item in value]
    if isinstance(value, str):
        if key:
            value = value.replace(key, '[密钥已隐藏]')
        value = _HEADER_SECRET.sub(r'\1[密钥已隐藏]', value)
        value = _BEARER.sub('Bearer [密钥已隐藏]', value)
        value = re.sub(
            r'(?i)([?&](?:api[_-]?key|key|token|access[_-]?token|secret|authorization|password)=)'
            r'[^&#\s"\'<>]+',
            r'\1[密钥已隐藏]', value,
        )
        value = _KEY.sub(r'\1\2[密钥已隐藏]', value)
        value = _URL.sub('[服务地址已隐藏]', value)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _redact_wire_text(value: str, key: str) -> str:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return str(_redact_audit_value(value, key))
    return json.dumps(
        _redact_audit_value(parsed, key), ensure_ascii=False, allow_nan=False,
    )


def _wire_response_evidence(result: Any, raw_text: str,
                            http_responses: list[dict] | None = None) -> dict:
    blocks = list(getattr(result, 'content', None) or [])
    structured = _structured_content(result)
    evidence = {
        'is_error': bool(getattr(result, 'isError', False)
                         or getattr(result, 'is_error', False)),
        'content_block_count': len(blocks),
        'content_block_types': [type(block).__name__ for block in blocks],
        'content_text_lengths': [len(str(getattr(block, 'text', ''))) for block in blocks],
        'raw_response_text': raw_text,
        'structured_content': structured,
        'http_responses': list(http_responses or []),
    }
    return evidence


def _find_decode_error(error: BaseException, seen: set[int] | None = None) -> SorftimeDecodeError | None:
    seen = seen or set()
    if id(error) in seen:
        return None
    seen.add(id(error))
    if isinstance(error, SorftimeDecodeError):
        return error
    for child in getattr(error, 'exceptions', ()) or ():
        found = _find_decode_error(child, seen)
        if found is not None:
            return found
    cause = error.__cause__ or error.__context__
    if cause is not None and cause is not error:
        return _find_decode_error(cause, seen)
    return None


def _record_exchange(name: str, arguments: dict, *, status: str, payload: object,
                     error: str, elapsed_seconds: float, api_key: str,
                     context: dict | None, started_at: str,
                     wire_response: dict | None = None) -> None:
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
        safe_wire_response = _redact_audit_value(wire_response, api_key) if wire_response else None
        if isinstance(safe_wire_response, dict) and isinstance(wire_response, dict):
            raw_wire_text = wire_response.get('raw_response_text')
            if isinstance(raw_wire_text, str):
                safe_wire_response['raw_response_text'] = _redact_wire_text(raw_wire_text, api_key)
        record = {
            'ordinal': stamp,
            'started_at': started_at,
            'tool': name,
            'request_arguments': _redact_audit_value(arguments, api_key),
            'execution_context': _redact_audit_value(context or {}, api_key),
            'status': status,
            'raw_payload': _redact_audit_value(payload, api_key),
            'wire_response': safe_wire_response,
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

    async def _call_live(self, name: str, arguments: dict) -> _LiveCallResult:
        # Sorftime's documented MCP transport authenticates with ?key=. Keep the
        # key out of snapshots, arguments, and diagnostics.
        from mcp.shared._httpx_utils import create_mcp_http_client

        separator = '&' if '?' in self.endpoint else '?'
        auth_endpoint = self.endpoint + separator + 'key=' + quote(self.api_key, safe='')
        http_responses: list[dict] = []

        async def capture_response(response) -> None:
            request = getattr(response, 'request', None)
            http_responses.append({
                'sequence': len(http_responses) + 1,
                'method': str(getattr(request, 'method', '')),
                'status_code': int(getattr(response, 'status_code', 0)),
                'content_type': str(getattr(response, 'headers', {}).get('content-type', '')),
            })

        response_audit = _LIVE_RESPONSE_AUDIT.get()
        if response_audit is not None:
            # The list is shared with the outer call's finally block, so a
            # transport failure still records HTTP status without response headers.
            response_audit['http_responses'] = http_responses

        http_client = create_mcp_http_client()
        http_client.event_hooks = {'response': [capture_response]}
        async with http_client:
            async with streamable_http_client(
                auth_endpoint, http_client=http_client, terminate_on_close=False,
            ) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
        raw_text = _result_text(result)
        # Keep parsing out of the streaming/session context. A decode exception
        # must not make async-generator cleanup obscure Sorftime's actual text.
        evidence = _wire_response_evidence(result, raw_text, http_responses)
        if response_audit is not None:
            response_audit.clear()
            response_audit.update(evidence)
        return _LiveCallResult(
            result=result,
            wire_response=evidence,
        )

    async def call(self, name: str, arguments: dict, *,
                   audit_context: dict | None = None) -> dict:
        from ..external_effects import assert_allowed
        assert_allowed('sorftime_mcp')
        if name not in ('category_tree', 'category_trend'):
            raise ValueError('不支持的 Sorftime 工具')
        deadline = None
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        payload = None
        wire_response = None
        audit_status = 'failed'
        audit_error = ''
        response_audit: dict = {}
        response_audit_token = _LIVE_RESPONSE_AUDIT.set(response_audit)
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
                if isinstance(result, _LiveCallResult):
                    wire_response = result.wire_response
                    allow_no_data_as_empty = _no_data_tree_is_eligible(
                        name, audit_context,
                    )
                    decoded = _decode(
                        result.result, key=self.api_key,
                        tool_name=name,
                        allow_no_data_as_empty=allow_no_data_as_empty,
                        wire_response=wire_response,
                    )
                    payload = decoded if isinstance(decoded, dict) else {'data': decoded}
                else:
                    payload = result if isinstance(result, dict) else {'data': result}
                # Some async clients suppress the cancellation used by
                # asyncio.timeout during transport cleanup. Do not accept a
                # late response merely because that client returned a value.
                if deadline.expired():
                    raise TimeoutError
            if name == 'category_tree' and not _has_tree_items(payload):
                raw_text = str((wire_response or {}).get('raw_response_text') or '')
                raise SorftimeDecodeError(
                    'Sorftime category_tree 返回了无法识别的 JSON 结构', raw_text,
                    stage='invalid_category_tree_shape', wire_response=wire_response,
                )
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
            decode_error = _find_decode_error(exc)
            if decode_error is not None:
                wire_response = decode_error.wire_response or wire_response
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
            decode_error = _find_decode_error(exc)
            if decode_error is not None:
                wire_response = decode_error.wire_response or wire_response
            if deadline is not None and deadline.expired():
                audit_error = (
                    f'Sorftime MCP 调用超过 {self.timeout_seconds:g} 秒总期限；底层异常：{diagnostic}'
                )
                raise RuntimeError(audit_error) from None
            audit_error = diagnostic
            raise RuntimeError(audit_error) from None
        finally:
            _LIVE_RESPONSE_AUDIT.reset(response_audit_token)
            if wire_response is None and response_audit.get('http_responses'):
                wire_response = {
                    'is_error': True,
                    'capture_stage': 'mcp_transport_failed_after_http_response',
                    'http_responses': list(response_audit['http_responses']),
                }
            _record_exchange(
                name, arguments, status=audit_status, payload=payload,
                error=audit_error, elapsed_seconds=time.monotonic() - started,
                api_key=self.api_key, context=audit_context, started_at=started_at,
                wire_response=wire_response,
            )


def _result_text(result: Any) -> str:
    return '\n'.join(str(getattr(part, 'text', '')) for part in (getattr(result, 'content', None) or []))


def _structured_content(result: Any) -> Any:
    value = getattr(result, 'structuredContent', None)
    return value if value is not None else getattr(result, 'structured_content', None)


def _no_data_tree_is_eligible(name: str, context: dict | None) -> bool:
    """Only a known, non-root node with a valid queued tree job can become a leaf."""
    if name != 'category_tree' or not isinstance(context, dict):
        return False
    try:
        trend_count = int(context.get('successful_trend_count') or 0)
    except (TypeError, ValueError):
        return False
    return (
        context.get('is_root') is False
        and context.get('node_known') is True
        and context.get('parent_edge_found') is True
        and context.get('tree_job_leased') is True
        and trend_count == 2
    )


def _decode(result: Any, *, key: str = '', tool_name: str = '',
            allow_no_data_as_empty: bool = False,
            wire_response: dict | None = None) -> dict | list:
    if (getattr(result, 'isError', False)
            or getattr(result, 'is_error', False)):
        raise RuntimeError(scrub(_result_text(result) or 'Sorftime MCP 工具返回错误', key))
    raw = _result_text(result)
    if raw.strip():
        if raw.strip().casefold() == 'no data available.':
            if isinstance(wire_response, dict):
                wire_response['official_sorftime_semantics_confirmed'] = False
                wire_response['parse_stage'] = 'no_data_sentinel_unconfirmed'
            if tool_name == 'category_tree' and allow_no_data_as_empty:
                if isinstance(wire_response, dict):
                    wire_response['parse_stage'] = 'no_data_sentinel_as_empty_child_array'
                    wire_response['normalization'] = 'exact_text_to_empty_direct_children_for_verified_non_root_node'
                return []
            if tool_name and tool_name != 'category_tree':
                raise SorftimeDecodeError(
                    f'Sorftime {tool_name} returned no data; response is unconfirmed',
                    raw, stage='no_data_response_unconfirmed', wire_response=wire_response,
                )
            raise SorftimeDecodeError(
                'Sorftime category_tree returned no data; child structure is unconfirmed',
                raw, stage='no_data_sentinel_unconfirmed', wire_response=wire_response,
            )
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Keep the baseline contract: malformed text is a failure even if
            # the server also supplied structuredContent. Preserve a short,
            # redacted excerpt because Sorftime may return useful plain-text
            # diagnostics such as its own soft-timeout message.
            if isinstance(wire_response, dict):
                wire_response['parse_stage'] = 'parse_tool_text_json'
            excerpt = scrub(raw, key)
            if excerpt:
                raise SorftimeDecodeError(
                    f'Sorftime MCP 返回非 JSON 文本：{excerpt}', raw,
                    stage='parse_tool_text_json', wire_response=wire_response,
                ) from None
            raise SorftimeDecodeError(
                'Sorftime MCP 返回非 JSON 文本', raw,
                stage='parse_tool_text_json', wire_response=wire_response,
            ) from None
    else:
        data = _structured_content(result)
        if data is None:
            if isinstance(wire_response, dict):
                wire_response['parse_stage'] = 'empty_tool_result'
            raise SorftimeDecodeError(
                'Sorftime MCP 返回空文本', raw,
                stage='empty_tool_result', wire_response=wire_response,
            )
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
