"""Agent event stream: turn the append-only agent_event log into an ordered SSE feed.

Pure protocol helpers are separated from the generator so cursor rules and the wire format can
be tested without a web server. One connection is one generator: no background thread, and the
store is queried per read instead of holding a transaction across a wait.
"""
import asyncio
import inspect
import json
import re
from . import events as store

SSE_EVENT_NAME='agent_event'
SSE_HEARTBEAT_SECONDS=15
SSE_POLL_INTERVAL=0.5
SSE_BATCH_LIMIT=200
CURSOR_PATTERN=re.compile(r'[0-9]+')


def resolve_cursor(query_value=None,header_value=None):
    """after_sequence query wins over Last-Event-ID; neither present means 0.

    A malformed cursor is an error rather than a silent fallback, otherwise a client would
    resume from the wrong place and either duplicate or miss events.
    """
    for raw in (query_value,header_value):
        if raw is None:continue
        text=str(raw).strip()
        if not text:continue
        if not CURSOR_PATTERN.fullmatch(text):raise ValueError('after_sequence 必须是非负整数')
        return int(text)
    return 0


def serialize_event(event):
    """One SSE frame. id is the sequence so a browser can resume with Last-Event-ID, and the
    whole event travels as single-line JSON so the payload is never hand-assembled."""
    return 'id: '+str(int(event['sequence']))+'\nevent: '+SSE_EVENT_NAME+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n'


def heartbeat_frame():
    """An SSE comment, not a fake event: no id, no sequence, cursor untouched."""
    return ': heartbeat\n\n'


async def event_stream(session_id,cursor=0,disconnected=None,sleep=None,
                       poll_seconds=SSE_POLL_INTERVAL,heartbeat_seconds=SSE_HEARTBEAT_SECONDS,batch_limit=SSE_BATCH_LIMIT):
    """Yield the backlog first without sleeping, then poll; emit heartbeats while idle.

    Only the store's read API is used, so sequence allocation stays in one place.
    """
    sleep=sleep or asyncio.sleep
    idle=0.0
    while True:
        if disconnected is not None:
            state=disconnected()
            if inspect.isawaitable(state):state=await state
            if state:return
        batch=store.read(session_id,after_sequence=cursor,limit=batch_limit)
        if batch:
            idle=0.0
            for event in batch:
                yield serialize_event(event)
                cursor=int(event['sequence'])
            continue
        await sleep(poll_seconds)
        idle+=poll_seconds
        if idle>=heartbeat_seconds:
            idle=0.0
            yield heartbeat_frame()
