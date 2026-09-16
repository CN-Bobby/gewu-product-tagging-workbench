"""P0 F02: slow turns must show honest progress."""
from backend.conversation_presentation import build


def test_presentation_exposes_progress_for_running_turns():
    events=[{'type':'tool','toolCallId':'a','title':'读取工作表','status':'completed','at':'2026-09-13T10:17:05+00:00'},
            {'type':'tool','toolCallId':'b','title':'读取工作表','status':'failed','at':'2026-09-13T10:17:20+00:00'},
            {'type':'tool','toolCallId':'c','title':'业务提问','status':'in_progress','at':'2026-09-13T10:17:25+00:00'}]
    presentation=build('',status='running',events=events,started_at='2026-09-13T10:17:01+00:00')
    progress=presentation['progress']
    assert progress['tool_calls']==3 and progress['failed_calls']==1
    assert progress['current_tool']=='业务提问'
    assert progress['started_at']=='2026-09-13T10:17:01+00:00'
    assert progress['last_activity']=='2026-09-13T10:17:25+00:00'


def test_presentation_without_events_has_no_progress():
    assert build('当前判断',status='completed')['progress'] is None


def test_tool_events_rebuild_presentation_and_expose_progress():
    events=[{'type':'tool','title':'正在读取工作台信息','status':'pending','toolCallId':'call-1',
             'at':'2026-09-13T10:17:05+00:00'}]
    presentation=build('',status='failed',events=events,started_at='2026-09-13T10:17:01+00:00')
    assert presentation['activity']
    assert presentation['progress']['tool_calls']==1
    assert presentation['progress']['current_tool']=='正在读取工作台信息'
