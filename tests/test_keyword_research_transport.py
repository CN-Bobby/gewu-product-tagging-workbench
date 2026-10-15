# -*- coding: utf-8 -*-
"""关键词调研的 MCP 传输层回归测试。

离线夹具（FakeMCP）覆写的正是 `_call_once`，也就是真实传输那一层，所以「SDK 的 yield
形状」这类只在真连服务器时才暴露的缺陷，用它永远抓不到——2026-09-23 的真实回合就栽在
这里：工作台锁定的 mcp 1.29.1 里 `streamable_http_client` 会 yield 三个值（多一个
session-id 取值器），而桌面版 pin 的 2.1.1 只有两个，按两个解包即抛
`ValueError: too many values to unpack (expected 2, got 3)`。

这里在回环地址上起一个最小 MCP 服务，用**生产** `MCPClient` 真打一次
initialize → tools/call → 解析，把这条缝钉住。只走回环，不碰外网，不读任何凭据。
"""
import asyncio
import json
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from backend.keyword_research.call_log import CallLog
from backend.keyword_research.mcp_client import MCPClient, MCPError, test_mcp_connection as probe_mcp_connection
from backend.keyword_research.rate_limiter import RateLimiter
from tests.test_keyword_research_pipeline import offline_only  # noqa: F401  离线护栏

pytestmark = pytest.mark.usefixtures("offline_only")


@pytest.fixture
def local_endpoint():
    """回环上的最小 MCP 服务：一个工具，回一个与 SellerSprite 同形状的响应。"""
    server = FastMCP("transport-probe")

    @server.tool()
    def product_node(request: dict) -> dict:
        return {"code": "OK", "message": "成功",
                "data": {"items": [{"asin": "X1"}], "total": 1,
                         "nodeIdPath": (request or {}).get("nodeIdPath")}}

    app = server.streamable_http_app()
    headers_seen = []

    from starlette.middleware.base import BaseHTTPMiddleware

    class HeaderRecorder(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            value = request.headers.get("secret-key")
            if value:
                headers_seen.append(value)
            return await call_next(request)

    app.add_middleware(HeaderRecorder)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True, name="local-mcp-server")
    thread.start()
    try:
        for _ in range(400):
            if running.started and getattr(running, "servers", None) and running.servers[0].sockets:
                break
            time.sleep(0.05)
        else:
            pytest.fail("本地 MCP 服务未在 20 秒内起来")
        port = running.servers[0].sockets[0].getsockname()[1]
        paths = [route.path for route in app.routes if getattr(route, "path", "").endswith("mcp")]
        path = paths[0] if paths else "/mcp"
        yield f"http://127.0.0.1:{port}{path}", headers_seen
    finally:
        running.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "本地 MCP 服务未能收工：测试不许泄漏线程"


def test_production_client_calls_a_streamable_http_server(local_endpoint, tmp_path):
    """生产传输路径端到端：解包、会话、调用、解析、调用日志，一次走完。"""
    log = CallLog(tmp_path / "call_log.json")
    endpoint, headers_seen = local_endpoint
    client = MCPClient(endpoint,
                       RateLimiter(max_calls=100, window_seconds=60.0, min_interval=0.0), log,
                       headers={"secret-key": "offline-header-probe"})

    data = asyncio.run(client.call("product_node", {"request": {"nodeIdPath": "1055398"}},
                                   "transport-probe"))

    assert data["code"] == "OK"
    assert data["data"]["items"][0]["asin"] == "X1"
    assert data["data"]["nodeIdPath"] == "1055398"
    assert headers_seen and set(headers_seen) == {"offline-header-probe"}
    checked = probe_mcp_connection(endpoint, timeout=10,
                                   rate_limiter=RateLimiter(max_calls=100, window_seconds=60.0, min_interval=0.0),
                                   headers={"secret-key": "offline-header-probe"})
    assert checked == {"ok": True, "tools": 1}
    assert set(headers_seen) == {"offline-header-probe"}

    recorded = log.as_dict() if hasattr(log, "as_dict") else None
    calls = recorded["calls"] if recorded else None
    if calls is None:
        import json
        calls = json.loads((tmp_path / "call_log.json").read_text(encoding="utf-8"))["calls"]
    assert [call["tool"] for call in calls] == ["product_node"]
    assert calls[0]["status"] == "success"


def test_nested_transport_group_is_actionable_redacted_and_not_retried(tmp_path, monkeypatch):
    from backend.keyword_research import mcp_client
    private_asin='B0'+'SYN00001'

    class FailedStream:
        async def __aenter__(self):
            raise ExceptionGroup('TaskGroup failures', [
                RuntimeError('ERROR_UNAUTHORIZED permission denied'),
                RuntimeError(f'invalid parameter for {private_asin} at https://example.invalid/mcp?secret-key=sk-private-credential'),
            ])
        async def __aexit__(self,*args):return False

    monkeypatch.setattr(mcp_client,'streamable_http_client',lambda *args,**kwargs:FailedStream())
    log=CallLog(tmp_path/'call_log.json')
    client=MCPClient('https://example.invalid/mcp',
                     RateLimiter(max_calls=100,window_seconds=60.0,min_interval=0.0),log,
                     max_temp_retries=1, headers={"secret-key": "sk-private-credential"})
    with pytest.raises(MCPError) as raised:
        asyncio.run(client.call('traffic_extend',{},'safe scope'))
    message=str(raised.value)
    assert 'ERROR_UNAUTHORIZED' in message and 'invalid parameter' in message
    assert private_asin not in message
    assert 'example.invalid' not in message and 'sk-private-credential' not in message
    assert raised.value.retryable is False and raised.value.unauthorized is True
    calls=log.load()['calls']
    assert len(calls)==1 and calls[0]['status']=='failed'
    assert private_asin not in json.dumps(calls)
    assert 'example.invalid' not in json.dumps(calls)
    assert 'sk-private-credential' not in json.dumps(calls)
    assert 'sk-private-credential' not in message


@pytest.mark.parametrize("retry_budget", [2, 10])
def test_task_retry_budget_is_cumulative_and_blocks_before_excess_request(
        tmp_path, monkeypatch, retry_budget):
    from backend.keyword_research import mcp_client

    class CountingLimiter:
        def __init__(self):
            self.attempts = 0

        def wait_until_allowed(self, on_wait=None):
            return 0

        def record_call(self):
            self.attempts += 1

    limiter = CountingLimiter()
    log = CallLog(tmp_path / f"calls-{retry_budget}.json")
    client = MCPClient("http://127.0.0.1/unused", limiter, log,
                       max_temp_retries=1, task_retry_budget=retry_budget)
    request_attempts = 0

    async def temporary_failure(tool, arguments):
        nonlocal request_attempts
        request_attempts += 1
        raise MCPError("temporary transport failure", retryable=True)

    monkeypatch.setattr(client, "_call_once", temporary_failure)
    for _ in range(retry_budget):
        with pytest.raises(MCPError, match="temporary transport failure"):
            asyncio.run(client.call("tool", {}, "task"))

    with pytest.raises(MCPError, match=f"已发起 {retry_budget} 次重试.*上限 {retry_budget} 次"):
        asyncio.run(client.call("tool", {}, "task"))

    # Each previous call made a base request and one retry. The blocked call
    # made its base request, but its next retry was rejected before transport.
    assert request_attempts == retry_budget * 2 + 1
    assert limiter.attempts == request_attempts  # failed attempts consume limiter capacity too
    assert client.task_retries_used == retry_budget
    assert len(log.load()["calls"]) == request_attempts


def test_recreated_client_restores_retries_already_spent_by_run(tmp_path, monkeypatch):
    log = CallLog(tmp_path / "restarted-run.json")
    seq = log.start_call("tool", "retry from previous Pipeline", is_retry=True)
    log.finish_call(seq, success=False, error="temporary transport failure")
    limiter = RateLimiter(max_calls=100, window_seconds=60.0, min_interval=0.0)
    client = MCPClient("http://127.0.0.1/unused", limiter, log, max_temp_retries=1)
    client.set_task_retry_budget(2, retries_used=1)
    request_attempts = 0

    async def temporary_failure(tool, arguments):
        nonlocal request_attempts
        request_attempts += 1
        raise MCPError("temporary transport failure", retryable=True)

    monkeypatch.setattr(client, "_call_once", temporary_failure)
    with pytest.raises(MCPError, match="temporary transport failure"):
        asyncio.run(client.call("tool", {}, "resumed task"))
    with pytest.raises(MCPError, match="已发起 2 次重试.*上限 2 次"):
        asyncio.run(client.call("tool", {}, "resumed task"))

    assert client.task_retries_used == 2
    assert request_attempts == 3  # one base + one retry, then one base; no third retry request
