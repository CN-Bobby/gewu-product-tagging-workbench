"""P0 F02: slow turns must show honest progress, and questions must not block the turn."""
import threading
import time
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend import business_questions,conversation,draft
from backend.conversation_presentation import build
from backend.db import Session,ChatQuestion,Message,WorkbenchMeta,require,uid
from backend.harness import HarnessError,instruction_hash


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


class ToolClient:
    closed=False
    proc=SimpleNamespace(poll=lambda:None)
    def __init__(self,on_event=None):self.on_event=on_event
    def prompt(self,text):
        self.on_event({'type':'tool','title':'正在读取工作台信息','status':'pending','toolCallId':'call-1','at':'2026-09-13T10:17:05+00:00'})
        raise HarnessError('turn_stalled','长时间没有进展，本轮已中止。',{})


def test_tool_events_rebuild_presentation_and_expose_progress(dataset,monkeypatch):
    did=dataset['id'];ident=uid()
    client=ToolClient();client.instruction_hash=instruction_hash()
    monkeypatch.setitem(conversation.clients,did,client)
    with Session.begin() as s:s.add(Message(id=ident,dataset_id=did,role='assistant',text='',status='running'))
    conversation.run(did,ident,'turn-progress','读一下工作表','test/model')
    with Session() as s:message=s.get(Message,ident)
    assert message.status=='failed' and message.error_code=='turn_stalled'
    assert message.presentation['activity']
    assert message.presentation['progress']['tool_calls']==1
    assert '长时间没有进展' in message.presentation['summary']


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


def test_answer_queues_exactly_one_followup_turn(dataset,monkeypatch):
    did=dataset['id'];start_turn(did)
    asked=business_questions.ask(did,'主色按什么口径？',['底色主导','整套视觉'])
    resumed=[]
    monkeypatch.setattr(conversation,'_resume_queued',lambda did,key:resumed.append((did,key)))
    monkeypatch.setattr(conversation.threading,'Thread',ImmediateThread)
    answered=business_questions.answer(did,asked['question_id'],['1'])
    assert answered['status']=='answered' and answered['selected_label']=='底色主导'
    with Session() as s:
        queued=s.get(WorkbenchMeta,'chat-followup:'+did)
        assert queued and '底色主导' in queued.data['text']
    assert resumed==[(did,'chat-followup:'+did)]


def test_answering_after_the_turn_ended_still_resumes(dataset,monkeypatch):
    did=dataset['id']
    with Session.begin() as s:
        message=Message(dataset_id=did,turn_id='turn-closed',role='assistant',text='',status='completed');s.add(message);s.flush()
        qid=uid()
        s.add(ChatQuestion(id=qid,dataset_id=did,turn_id='turn-closed',message_id=message.id,
            prompt='尺寸段如何划分？',options=[{'option_id':'1','label':'固定边界'},{'option_id':'2','label':'按批次等分'}],status='pending'))
        s.add(WorkbenchMeta(id='business-question:'+qid,dataset_id=did,kind='business_question',data={'multiple':False,'answer':None}))
    resumed=[]
    monkeypatch.setattr(conversation,'_resume_queued',lambda did,key:resumed.append(key))
    monkeypatch.setattr(conversation.threading,'Thread',ImmediateThread)
    answered=business_questions.answer(did,qid,['2'])
    assert answered['status']=='answered' and answered['selected_label']=='按批次等分'
    with Session() as s:assert s.get(WorkbenchMeta,'chat-followup:'+did)
    assert resumed==['chat-followup:'+did]
