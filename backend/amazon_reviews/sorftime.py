"""Small Amazon-specific Sorftime Streamable HTTP adapter.

This module is intentionally separate from the Xunlongjue adapter because that
adapter's tool allowlist is scoped to the category-tree workflow.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
import time
from typing import Any
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .. import usage as usage_stats

TOOLS = {"category_report", "product_reviews"}
MAX_CONNECT_RETRIES = 1
CONNECT_RETRY_DELAY_SECONDS = 0.5
_USAGE_CONTEXT = ContextVar("sorftime_usage_context", default=None)
_SECRET = re.compile(r"(?i)(key|token|secret)([=: ]+)[^&\s,;]+")
_AUTH_QUERY = re.compile(r"(?i)([?&](?:key|api[_-]?key|secret-key)=)[^&\s]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _diagnostic(value: BaseException) -> str:
    if isinstance(value, BaseExceptionGroup):
        return "; ".join(_diagnostic(child) for child in value.exceptions)
    return f"{type(value).__name__}: {value}"


def _is_connect_failure(value: BaseException) -> bool:
    if isinstance(value, BaseExceptionGroup):
        return any(_is_connect_failure(child) for child in value.exceptions)
    return type(value).__name__ in {"ConnectError", "ConnectTimeout"} or isinstance(value, ConnectionRefusedError)


def _scrub(value: Any, key: str) -> str:
    message = str(value or "Sorftime call failed")
    if key:
        message = message.replace(key, "[hidden]")
    message = _AUTH_QUERY.sub(r"\1[hidden]", message)
    message = _BEARER.sub("Bearer [hidden]", message)
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _non_json_error(raw: str, blocks: list[Any], structured_content: Any) -> ValueError:
    stripped = raw.lstrip().lower()
    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        format_hint = "HTML response"
    elif stripped.startswith(("event:", "data:", "id:", "retry:")):
        format_hint = "SSE-like response"
    else:
        format_hint = "unrecognized text format"
    block_types = ",".join(type(block).__name__ for block in blocks) or "none"
    structured_state = "present" if structured_content is not None else "absent"
    return ValueError(
        "Sorftime returned non-JSON review data "
        f"({format_hint}; content_blocks={len(blocks)}; block_types={block_types}; "
        f"structured_content={structured_state})"
    )


def _decode(result: Any, key: str, *, tool_name: str = "") -> Any:
    blocks = list(getattr(result, "content", None) or [])
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raw = " ".join(str(getattr(part, "text", "")) for part in blocks)
        raise RuntimeError(_SECRET.sub(r"\1\2[hidden]", raw.replace(key, "[hidden]"))[:400] or "Sorftime tool failed")

    structured_content = getattr(result, "structuredContent", None)
    if structured_content is None:
        structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        return structured_content

    text_blocks = [str(getattr(block, "text", "")) for block in blocks
                   if str(getattr(block, "text", "")).strip()]
    raw = "\n".join(text_blocks)
    if raw.strip():
        if tool_name == "product_reviews" and re.fullmatch(
                r"no reviews found\.?", raw.strip(), flags=re.IGNORECASE):
            return {"data": [], "message": raw.strip()}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            if len(text_blocks) > 1:
                try:
                    parsed_blocks = [json.loads(block_text) for block_text in text_blocks]
                except (ValueError, TypeError):
                    raise _non_json_error(raw, blocks, structured_content) from None
                merged_rows = None
                merged_field = ""
                if all(isinstance(value, list) for value in parsed_blocks):
                    merged_field = "data"
                    merged_rows = [row for value in parsed_blocks for row in value]
                elif all(isinstance(value, dict) for value in parsed_blocks):
                    for field in ("reviews", "items", "data", "result", "list", "records"):
                        values = [value.get(field) for value in parsed_blocks]
                        if all(isinstance(value, list) for value in values):
                            merged_field = field
                            merged_rows = [row for value in values for row in value]
                            break
                if merged_rows is not None:
                    # Keep each source block for auditability and add the
                    # combined list for the service's normal payload walker.
                    return {
                        merged_field: merged_rows,
                        "content": [{"text": block_text} for block_text in text_blocks],
                    }
                raise _non_json_error(raw, blocks, structured_content) from None
            raise _non_json_error(raw, blocks, structured_content) from None
    raise ValueError("Sorftime returned an empty result")


async def _call(endpoint: str, key: str, name: str, arguments: dict[str, Any]) -> Any:
    from ..external_effects import assert_allowed
    assert_allowed('amazon_reviews_sorftime')
    if name not in TOOLS:
        raise ValueError("Unsupported Sorftime tool")
    auth_endpoint = endpoint + ("&" if "?" in endpoint else "?") + "key=" + quote(key, safe="")
    async with streamable_http_client(auth_endpoint, terminate_on_close=False) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            return _decode(result, key, tool_name=name)


@contextmanager
def usage_scope(context: dict[str, Any]):
    token = _USAGE_CONTEXT.set(dict(context))
    try:
        yield
    finally:
        _USAGE_CONTEXT.reset(token)


def call(endpoint: str, key: str, name: str, arguments: dict[str, Any], *,
         usage_context: dict[str, Any] | None = None) -> Any:
    context = usage_context or _USAGE_CONTEXT.get() or {}
    event_id = usage_stats.start_usage_event(
        kind="mcp", module=str(context.get("module") or "amazon_reviews"),
        task_id=context.get("task_id"), task_title=context.get("task_title"),
        function_name=name, server_name="Sorftime", attempt=context.get("attempt", 1),
        metadata={key: value for key, value in context.items()
                  if key not in {"module", "task_id", "task_title", "attempt"}},
    )
    started = time.perf_counter()
    retries = 0
    while True:
        try:
            result = asyncio.run(asyncio.wait_for(_call(endpoint, key, name, arguments), timeout=180.0))
            break
        except TimeoutError:
            usage_stats.finish_usage_event(event_id, status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                metadata={"transport_retries": retries})
            raise RuntimeError("Sorftime MCP call exceeded 180 seconds") from None
        except Exception as exc:  # noqa: BLE001
            if retries < MAX_CONNECT_RETRIES and _is_connect_failure(exc):
                retries += 1
                time.sleep(CONNECT_RETRY_DELAY_SECONDS * retries)
                continue
            usage_stats.finish_usage_event(event_id, status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                metadata={"transport_retries": retries})
            raise RuntimeError(_scrub(_diagnostic(exc), key)) from None
    usage_stats.finish_usage_event(event_id, status="success",
        duration_ms=round((time.perf_counter() - started) * 1000),
        metadata={"transport_retries": retries})
    return result
