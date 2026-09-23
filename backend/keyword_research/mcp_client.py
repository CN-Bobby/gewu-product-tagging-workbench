# -*- coding: utf-8 -*-
"""SellerSprite MCP 客户端（Streamable HTTP）。

所有业务调用统一经过：限流器等待 → call_log.start_call(in_progress) →
session.call_tool → 原样解析落盘（由调用方写 raw/）→ call_log.finish_call。
临时错误自动重试 1 次；权限/额度/参数错误不重试（skill 纪律）。
"""
from __future__ import annotations

import asyncio
import json
import re
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
    "invalid param",
    "invalid_argument",
    "bad request",
    "参数错误",
    "无权限",
    "额度",
    "invalid_parameter",
)

_ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b")


def _sanitize_diagnostic(text: str) -> str:
    text = _BEARER_RE.sub("Bearer [已隐藏]", str(text))
    text = _KEY_RE.sub("[密钥已隐藏]", text)
    text = _URL_RE.sub("[服务地址已隐藏]", text)
    text = _ASIN_RE.sub("[ASIN已隐藏]", text)
    return text.replace("\r", " ").replace("\n", " ")[:500]


def diagnostic_for_exception(exc: BaseException) -> str:
    """Unwrap concurrent transport failures into a safe, actionable message."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        parts = []
        for child in nested:
            message = diagnostic_for_exception(child)
            if message and message not in parts:
                parts.append(message)
        if parts:
            return "并发请求失败：" + "；".join(parts)
    message = _sanitize_diagnostic(str(exc).strip())
    return message or type(exc).__name__


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
        task_retry_budget: int = 10,
    ) -> None:
        self.endpoint = endpoint
        self.rate_limiter = rate_limiter
        self.call_log = call_log
        self.on_event = on_event
        self.max_temp_retries = max_temp_retries
        self.request_timeout = request_timeout
        self.rate_limit_retry_wait = rate_limit_retry_wait
        self.task_retry_budget = max(0, int(task_retry_budget))
        self.task_retries_used = 0

    def set_task_retry_budget(self, retry_budget: int, retries_used: int = 0) -> None:
        """Set the confirmed plan's allowance and retries already recorded for this run."""
        self.task_retry_budget = max(0, int(retry_budget))
        self.task_retries_used = max(0, int(retries_used))

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
            # 只取读写两条流：mcp 1.29.1 还会多 yield 一个 session-id 取值器（2.x 没有），
            # 这里对两者都成立，不需要固定解包长度。每次调用各自开一个会话，用不到它。
            async with streamable_http_client(self.endpoint, terminate_on_close=False) as (
                read,
                write,
                *_,
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
        except MCPError as exc:
            raise MCPError(_sanitize_diagnostic(str(exc)),
                           retryable=exc.retryable, unauthorized=exc.unauthorized,
                           rate_limited=exc.rate_limited) from None
        except Exception as exc:  # transport wrappers may themselves be ExceptionGroups
            message = diagnostic_for_exception(exc)
            raise MCPError(f"传输/并发请求错误: {message}",
                           retryable=self._is_retryable(message),
                           unauthorized=self._is_unauthorized(message),
                           rate_limited=_is_rate_limit_text(message)) from None

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
            if attempt > 0:
                # Enforce before waiting or making the next network request.
                if self.task_retries_used >= self.task_retry_budget:
                    raise MCPError(
                        f"任务临时重试预算已用尽：已发起 {self.task_retries_used} 次重试，"
                        f"上限 {self.task_retry_budget} 次；已阻止 {tool} 的后续重试请求。"
                    ) from last_error
            wait = self.rate_limiter.wait_until_allowed(
                on_wait=lambda w: self._emit("rate_limit_wait", wait_seconds=w, tool=tool, scope=scope)
            )
            if wait > 0:
                self._emit("rate_limit_passed", waited_seconds=wait, tool=tool)
            # Recheck after the limiter await: concurrent calls may have all
            # passed the early check while waiting, but each must reserve its
            # retry synchronously before starting the next request.
            if attempt > 0 and self.task_retries_used >= self.task_retry_budget:
                raise MCPError(
                    f"任务临时重试预算已用尽：已发起 {self.task_retries_used} 次重试，"
                    f"上限 {self.task_retry_budget} 次；已阻止 {tool} 的后续重试请求。"
                ) from last_error
            seq = self.call_log.start_call(
                tool, scope,
                is_retry=attempt > 0,
                retry_reason=("临时错误重试" if attempt > 0 else None),
                planned_total=planned_total,
                hard_cap=hard_cap,
            )
            self._emit("call_start", seq=seq, tool=tool, scope=scope)
            if attempt > 0:
                # This synchronous section has no await, so concurrent coroutines
                # cannot spend the same retry slot twice. Count only attempts that
                # actually reach the request boundary.
                self.task_retries_used += 1
            # Count every request attempt, including one that fails in transport
            # or at the business layer, so the limiter reflects actual traffic.
            self.rate_limiter.record_call()
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
                        # 服务端限流（如“接口访问次数已达上限”）：本地限流
                        # 无法覆盖服务端口径，等待配置时长后重试
                        retry_wait = self.rate_limit_retry_wait
                        self._emit("rate_limit_wait", wait_seconds=retry_wait,
                                   tool=tool, scope=scope, source="server")
                        await asyncio.sleep(retry_wait)
                    continue
                raise
            finally:
                pass
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

    连接测试也走限流器（默认模块级限流器，60次/滚动60秒，间隔至少1秒）。
    """
    import asyncio

    limiter = rate_limiter or _DEFAULT_TEST_LIMITER

    async def _run() -> int:
        limiter.wait_until_allowed()
        try:
            async with streamable_http_client(endpoint, terminate_on_close=False) as (
                read,
                write,
                *_,
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
