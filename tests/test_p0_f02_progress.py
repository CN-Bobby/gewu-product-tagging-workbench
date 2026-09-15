"""P0 F02: slow turns must show honest progress, and questions must not block the turn."""
import threading
import time
import pytest
from sqlalchemy import select
from backend import business_questions,draft
from backend.conversation_presentation import build
from backend.db import Session,ChatQuestion,Message,WorkbenchMeta,require,uid


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


def start_turn(did):
    mid=uid();turn=uid()
    with Session.begin() as s:
        s.add(Message(id=mid,dataset_id=did,role='assistant',text='',status='running'))
        s.add(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',data={'turn_id':turn,'message_id':mid}))
    return mid,turn


def test_ask_persists_and_returns_without_blocking_the_turn(dataset):
    did=dataset['id'];start_turn(did)
    started=time.monotonic()
    result=business_questions.ask(did,'“U领运动”和“U领吊带”如何区分？',['看肩带宽窄','看整体运动感'])
    assert time.monotonic()-started<1.0
    assert result['status']=='pending' and result['question_id']
    assert '不要重复提问' in result['instruction']
    with Session() as s:
        question=require(s,ChatQuestion,result['question_id'])
        assert question.status=='pending' and question.options[0]['label']=='看肩带宽窄'
        assert s.get(WorkbenchMeta,'business-question:'+question.id) is not None


def test_ask_refuses_to_stack_questions_on_one_turn(dataset):
    did=dataset['id'];start_turn(did)
    business_questions.ask(did,'第一个问题？',['甲','乙'])
    with pytest.raises(ValueError):business_questions.ask(did,'第二个问题？',['甲','乙'])


class ImmediateThread:
    def __init__(self,target=None,args=(),daemon=None,**kwargs):self.target=target;self.args=args
    def start(self):self.target(*self.args)
