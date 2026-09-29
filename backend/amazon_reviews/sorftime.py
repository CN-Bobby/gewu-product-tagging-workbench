"""Small Amazon-specific Sorftime Streamable HTTP adapter.

This module is intentionally separate from the Xunlongjue adapter because that
adapter's tool allowlist is scoped to the category-tree workflow.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOOLS = {"category_report", "product_reviews"}
_SECRET = re.compile(r"(?i)(key|token|secret)([=: ]+)[^&\s,;]+")
_AUTH_QUERY = re.compile(r"(?i)([?&](?:key|api[_-]?key|secret-key)=)[^&\s]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _diagnostic(value: BaseException) -> str:
    if isinstance(value, BaseExceptionGroup):
        return "; ".join(_diagnostic(child) for child in value.exceptions)
    return f"{type(value).__name__}: {value}"


def _scrub(value: Any, key: str) -> str:
    message = str(value or "Sorftime call failed")
    if key:
        message = message.replace(key, "[hidden]")
    message = _AUTH_QUERY.sub(r"\1[hidden]", message)
    message = _BEARER.sub("Bearer [hidden]", message)
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _decode(result: Any, key: str) -> Any:
    if getattr(result, "isError", False):
        raw = " ".join(str(getattr(part, "text", "")) for part in (getattr(result, "content", None) or []))
        raise RuntimeError(_SECRET.sub(r"\1\2[hidden]", raw.replace(key, "[hidden]"))[:400] or "Sorftime tool failed")
    raw = "\n".join(str(getattr(part, "text", "")) for part in (getattr(result, "content", None) or []))
    if raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("Sorftime returned non-JSON review data") from None
    value = getattr(result, "structuredContent", None)
    if value is None:
        raise ValueError("Sorftime returned an empty result")
    return value


async def _call(endpoint: str, key: str, name: str, arguments: dict[str, Any]) -> Any:
    if name not in TOOLS:
        raise ValueError("Unsupported Sorftime tool")
    auth_endpoint = endpoint + ("&" if "?" in endpoint else "?") + "key=" + quote(key, safe="")
    async with streamable_http_client(auth_endpoint, terminate_on_close=False) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            return _decode(result, key)


def call(endpoint: str, key: str, name: str, arguments: dict[str, Any]) -> Any:
    try:
        return asyncio.run(asyncio.wait_for(_call(endpoint, key, name, arguments), timeout=180.0))
    except ExceptionGroup as exc:
        raise RuntimeError(_scrub(_diagnostic(exc), key)) from None
    except TimeoutError:
        raise RuntimeError("Sorftime MCP call exceeded 180 seconds") from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_scrub(exc, key)) from None
