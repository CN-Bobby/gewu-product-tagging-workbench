"""运行事件流：把追加式运行日志变成有序的 SSE 供给。

协议助手与生成器分开，游标规则和线上格式可以不启动 Web 服务就测到（与 agent/stream.py
同一纪律）。一条连接就是一个生成器，没有后台线程，每次读取都重新查日志，不跨等待持锁。
"""
import asyncio
import inspect
import json
import re
from . import registry

SSE_EVENT_NAME = 'run_event'
SSE_HEARTBEAT_SECONDS = 15
SSE_POLL_INTERVAL = 0.5
SSE_BATCH_LIMIT = 200
CURSOR_PATTERN = re.compile(r'[0-9]+')
TERMINAL_EVENT_TYPES = frozenset({'run_finished', 'run_failed', 'run_cancelled'})


def resolve_cursor(query_value=None, header_value=None):
    """after_sequence 查询参数优先于 Last-Event-ID；都没有就是 0。

    游标格式不对按错误处理而不是静默回落，否则客户端会从错的位置续读，重复或漏事件。
    """
    for raw in (query_value, header_value):
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if not CURSOR_PATTERN.fullmatch(text):
            raise ValueError('after_sequence 必须是非负整数')
        return int(text)
    return 0


def serialize_event(event):
    """一帧 SSE。id 就是 sequence，浏览器可用 Last-Event-ID 续读；事件整体走单行 JSON，
    载荷不手工拼装。"""
    return 'id: ' + str(int(event['sequence'])) + '\nevent: ' + SSE_EVENT_NAME + '\ndata: ' + \
        json.dumps(event, ensure_ascii=False) + '\n\n'


def heartbeat_frame():
    """SSE 注释，不是假事件：没有 id、没有 sequence，游标不动。"""
    return ': heartbeat\n\n'


async def event_stream(run_id, cursor=0, disconnected=None, sleep=None,
                       poll_seconds=SSE_POLL_INTERVAL, heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
                       batch_limit=SSE_BATCH_LIMIT):
    """先不睡地把积压事件吐完，再轮询；空闲时发心跳。运行到达终态并吐完后收流。"""
    sleep = sleep or asyncio.sleep
    idle = 0.0
    while True:
        if disconnected is not None:
            state = disconnected()
            if inspect.isawaitable(state):
                state = await state
            if state:
                return
        batch = registry.read_events(run_id, after_sequence=cursor, limit=batch_limit)
        if batch:
            idle = 0.0
            for event in batch:
                yield serialize_event(event)
                cursor = int(event['sequence'])
                if event['event_type'] in TERMINAL_EVENT_TYPES:
                    return
            continue
        await sleep(poll_seconds)
        idle += poll_seconds
        if idle >= heartbeat_seconds:
            idle = 0.0
            yield heartbeat_frame()
