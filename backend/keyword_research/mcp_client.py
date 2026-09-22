# -*- coding: utf-8 -*-
"""SellerSprite MCP 客户端（Streamable HTTP）。

所有业务调用统一经过：限流器等待 → call_log.start_call(in_progress) →
session.call_tool → 原样解析落盘（由调用方写 raw/）→ call_log.finish_call。
临时错误自动重试 1 次；权限/额度/参数错误不重试（skill 纪律）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .call_log import CallLog
from .rate_limiter import RateLimiter

# 不重试的错误特征（skill 纪律：权限/额度/参数错误不消耗重试预算）
NO_RETRY_MARKERS = (
    "ERROR_UNAUTHORIZED",
    "unauthorized",
    "permission",
    "invalid parameter",
    "invalid_argument",
    "bad request",
    "参数错误",
    "无权限",
    "额度",
)


class MCPError(Exception):
    """MCP 调用失败（含业务错误与传输错误）。"""

    def __init__(self, message: str, *, retryable: bool = False,
                 unauthorized: bool = False, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.unauthorized = unauthorized
        self.rate_limited = rate_limited


def _is_rate_limit_text(text: str) -> bool:
    low = text.lower()
    markers = ("已达上限", "限流", "rate limit", "too many", "频繁", "访问次数")
    return any(m in low or m in text for m in markers)


class MCPClient:
    def __init__(
        self,
        endpoint: str,
        rate_limiter: RateLimiter,
        call_log: CallLog,
        on_event: Optional[Callable[[dict], None]] = None,
        max_temp_retries: int = 1,
        request_timeout: float = 180.0,
        rate_limit_retry_wait: float = 60.0,
    ) -> None:
        self.endpoint = endpoint
        self.rate_limiter = rate_limiter
        self.call_log = call_log
        self.on_event = on_event
        self.max_temp_retries = max_temp_retries
        self.request_timeout = request_timeout
        self.rate_limit_retry_wait = rate_limit_retry_wait

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"kind": kind, **payload})
            except Exception:
                pass

    def _decode_result(self, result: Any) -> dict:
        """把 CallToolResult 转成 dict。TextContent 是 JSON 字符串。"""
        content = getattr(result, "content", None) or []
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        raw = "\n".join(parts)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 非 JSON 文本：包一层便于调用方处理
            return {"_raw_text": raw}

    async def _call_once(self, tool: str, arguments: dict) -> dict:
        """单次调用（不重试）。调用方负责限流等待与日志。"""
        try:
            async with streamable_http_client(self.endpoint, terminate_on_close=False) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments=arguments)
                    if getattr(result, "isError", False):
                        err_text = self._describe_error(result)
                        raise MCPError(err_text, retryable=self._is_retryable(err_text),
                                       unauthorized=self._is_unauthorized(err_text))
                    data = self._decode_result(result)
                    self._raise_business_error(data)
                    return data
        except MCPError:
            raise
        except Exception as exc:  # 传输层异常视为临时错误
            raise MCPError(f"传输错误: {exc}", retryable=True) from exc

    @staticmethod
    def _raise_business_error(data: dict) -> None:
        """识别响应体中的业务错误（code!=OK / message 含错误文本）。

        实测：SellerSprite 对超频/限流返回 code=OK 但 message="接口访问次数已达上限"，
        若当作成功落盘会导致下游"无候选关键词"等误导性失败——必须在此拦截。
        """
        if not isinstance(data, dict):
            return
        code = data.get("code")
        message = str(data.get("message") or "")
        if code is not None and code != "OK":
            raise MCPError(f"业务错误 code={code}: {message}",
                           retryable=_is_rate_limit_text(message),
                           unauthorized="UNAUTHORIZED" in str(code).upper(),
                           rate_limited=_is_rate_limit_text(message))
        if _is_rate_limit_text(message) or "已达上限" in message:
            raise MCPError(f"服务端限流: {message}", retryable=True,
                           rate_limited=True)

    @staticmethod
    def _describe_error(result: Any) -> str:
        parts = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts) or f"MCP 返回错误 isError=true"

    @staticmethod
    def _is_retryable(text: str) -> bool:
        low = text.lower()
        return not any(m in low for m in NO_RETRY_MARKERS)

    @staticmethod
    def _is_unauthorized(text: str) -> bool:
        return "ERROR_UNAUTHORIZED" in text or "unauthorized" in text.lower()

    async def call(self, tool: str, arguments: dict, scope: str,
                   planned_total: int | None = None,
                   hard_cap: int | None = None) -> dict:
        """限流 + 日志 + 调用 + 临时重试的完整入口。返回解析后的 dict。"""
        attempt = 0
        last_error: Optional[MCPError] = None
        while True:
            wait = self.rate_limiter.wait_until_allowed(
                on_wait=lambda w: self._emit("rate_limit_wait", wait_seconds=w, tool=tool, scope=scope)
            )
            if wait > 0:
                self._emit("rate_limit_passed", waited_seconds=wait, tool=tool)
            seq = self.call_log.start_call(
                tool, scope,
                is_retry=attempt > 0,
                retry_reason=("临时错误重试" if attempt > 0 else None),
                planned_total=planned_total,
                hard_cap=hard_cap,
            )
            self._emit("call_start", seq=seq, tool=tool, scope=scope)
            try:
                data = await self._call_once(tool, arguments)
            except MCPError as exc:
                last_error = exc
                returned = None
                self.call_log.finish_call(seq, success=False, error=str(exc))
                self._emit("call_error", seq=seq, tool=tool, error=str(exc),
                           retryable=exc.retryable)
                if exc.retryable and attempt < self.max_temp_retries:
                    attempt += 1
                    self._emit("call_retry", seq=seq, tool=tool, attempt=attempt)
                    if exc.rate_limited:
                        # 服务端限流（如“接口访问次数已达上限”）：本地 8 次/分钟
                        # 无法覆盖服务端口径，等待 60 秒后重试
                        retry_wait = self.rate_limit_retry_wait
                        self._emit("rate_limit_wait", wait_seconds=retry_wait,
                                   tool=tool, scope=scope, source="server")
                        await asyncio.sleep(retry_wait)
                    continue
                raise
            finally:
                pass
            self.rate_limiter.record_call()
            returned = self._count_items(data)
            truncated = self._detect_truncation(data)
            self.call_log.finish_call(seq, success=True, returned=returned,
                                      truncated=truncated)
            self._emit("call_success", seq=seq, tool=tool, returned=returned,
                       truncated=truncated)
            return data

    @staticmethod
    def _count_items(data: dict) -> Optional[int]:
        d = data.get("data")
        if isinstance(d, dict):
            items = d.get("items")
            if isinstance(items, list):
                return len(items)
        elif isinstance(d, list):
            return len(d)
        return None

    @staticmethod
    def _detect_truncation(data: dict) -> bool:
        d = data.get("data")
        if isinstance(d, dict):
            items = d.get("items")
            if isinstance(items, list):
                total = d.get("total")
                if isinstance(total, int) and total > 0 and len(items) < total:
                    return True
        return False

    # ---------- 业务工具 ----------

    async def product_node(self, marketplace: str, node_id_path: str,
                           month: str | None = None) -> dict:
        args: dict[str, Any] = {"request": {"marketplace": marketplace,
                                            "nodeIdPath": node_id_path}}
        if month:
            args["request"]["month"] = month
        return await self.call("product_node", args, f"node {node_id_path}")

    async def product_research(self, marketplace: str, node_id_path: str,
                               month: str, page: int, size: int = 50) -> dict:
        """Node 榜单取样。skill 实测纪律：nodeId 用完整 path、必须 variation=Y。"""
        return await self.call(
            "product_research",
            {"request": {
                "marketplace": marketplace,
                "nodeIdPath": node_id_path,
                "month": month,
                "page": page,
                "size": size,
                "variation": "Y",
            }},
            f"products {node_id_path} p{page}",
        )

    async def traffic_extend(self, marketplace: str, asins: list[str],
                             page: int, size: int = 50,
                             history_date: str | None = None,
                             query_type: int = 2) -> dict:
        """批量反查。queryType 默认 2（当前榜单，官方 schema 默认值）。

        实测根因（2026-08-29）：queryType=1（畅销变体）返回的是变体组销售
        最佳成员的流量词，与 traffic_keyword（本 ASIN 精确词）词集错位，
        导致“指定 ASIN 清单”场景矩阵交集为 0、报告矩阵空白；
        queryType=2 与 traffic_keyword 词集 100% 一致（15/15 验证）。
        """
        args: dict[str, Any] = {
            "request": {
                "marketplace": marketplace,
                "asinList": asins,
                "queryType": query_type,
                "page": page,
                "size": size,
                "order": {"field": "trafficPercentage", "desc": True},
            }
        }
        if history_date:
            args["request"]["historyDate"] = history_date
        return await self.call(
            "traffic_extend", args,
            f"{marketplace} batch ({len(asins)} asins) p{page}",
        )

    async def traffic_keyword(self, marketplace: str, asin: str,
                              page: int = 1, size: int = 100,
                              month: str | None = None) -> dict:
        """指定 ASIN 流量关键词矩阵。

        month=None（默认）：近 30 天实时榜单（兼容现状）；
        month=yyyyMM：指定历史月份快照（当月快照未归档时服务端返回 total=0，
        属 SellerSprite 机制，由上层防呆处理）。
        """
        request = {
            "marketplace": marketplace,
            "asin": asin,
            "page": page,
            "size": size,
            "order": {"field": "trafficPercentage", "desc": True},
        }
        scope = f"{marketplace} current asin {asin} page={page}"
        if month:
            request["month"] = month
            scope = f"{marketplace} month={month} asin {asin} page={page}"
        return await self.call("traffic_keyword", {"request": request}, scope)

    async def aba_research_trend(self, marketplace: str, keyword: str) -> dict:
        return await self.call(
            "aba_research_trend",
            {"marketplace": marketplace, "keyword": keyword,
             "timeGranularity": "M"},
            f"{marketplace} trend {keyword}",
        )


def test_mcp_connection(endpoint: str, timeout: float = 60.0,
                        rate_limiter: RateLimiter | None = None) -> dict:
    """设置页“测试连接”：list_tools 验证凭据与连通性（不产生业务调用）。

    按限流纪律：连接测试也走限流器（默认模块级限流器，8次/60s）。
    """
    import asyncio

    limiter = rate_limiter or _DEFAULT_TEST_LIMITER

    async def _run() -> int:
        limiter.wait_until_allowed()
        try:
            async with streamable_http_client(endpoint, terminate_on_close=False) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return len(tools.tools)
        finally:
            limiter.record_call()

    try:
        count = asyncio.run(asyncio.wait_for(_run(), timeout=timeout))
        return {"ok": True, "tools": count}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


# 模块级默认限流器（测试连接共享，防止连点高频）
_DEFAULT_TEST_LIMITER = RateLimiter()
