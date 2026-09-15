"""M5-1: a persisted business question belongs to the runtime that asked it.

Answering one resumes the asking conversation, so a runtime with no such conversation must not be
able to create one. The two chains are kept apart by capability rather than by advice: the agent
runtime never registers the tool, and the clarification path it does have — an interaction on its
own session — can only reach its own session.

The point of these tests is the boundary, not the mechanism: legacy still resumes legacy, an agent
answer never touches the legacy orchestrator, and no configuration drift can hand the tool back.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import agent_application,business_questions,chat_threads,draft,harness
from backend.agent import events as agent_store
from backend.app import app
from backend.db import Message,Session,WorkbenchMeta,require,uid
from fake_backend import FakeBackend

ROOT=Path(__file__).resolve().parents[1]
AGENT_MODULES=['agent/backend.py','agent/bridge.py','agent/events.py','agent/protocol.py','agent/reasonix.py',
               'agent/runtime.py','agent/stream.py','agent_application.py','agent_http.py']


@pytest.fixture
def client():
    with TestClient(app) as test_client:yield test_client


def wait_for(produce,timeout=10):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=produce()
        if value:return value
        time.sleep(0.02)
    return None


def seed_legacy_turn(did,name='问题归属测试'):
    """A legacy chat turn in flight: the owner marker a business question is attached to."""
    thread=chat_threads.create(did,name)
    turn=uid();message_id=uid()
    with Session.begin() as s:
        s.add(WorkbenchMeta(id='chat-turn:'+turn,dataset_id=did,kind='chat_turn',
                            data={'turn_id':turn,'conversation_id':thread['id']}))
        s.add(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',
                            data={'turn_id':turn,'message_id':message_id,'conversation_id':thread['id']}))
        s.add(Message(id=message_id,dataset_id=did,role='assistant',text='',status='running'))
    return {'turn_id':turn,'message_id':message_id,'conversation_id':thread['id']}


def install_agent_backend(monkeypatch,mode='ask_interaction'):
    """Drive the whole agent HTTP surface with a deterministic double, everything else real."""
    created=[]
    def factory(dataset_id,model):
        def build():
            backend=FakeBackend(mode=mode)
            created.append(backend)
            return backend
        return build
    monkeypatch.setattr(agent_application,'backend_factory',factory)
    monkeypatch.setattr(agent_application,'_BRIDGES',{})
    monkeypatch.setattr(agent_application,'_SESSIONS',{})
    return created


def start_agent_session(client,did):
    session=client.post('/api/agent/sessions',json={'dataset_id':did})
    assert session.status_code==200,session.text
    session_id=session.json()['id']
    assert client.post('/api/agent/sessions/%s/start'%session_id).status_code==200
    return session_id


def agent_interaction(client,session_id,text='需要确认口径'):
    """Run one turn whose question arrives as an interaction on the session."""
    assert client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':text}).status_code==200
    announced=wait_for(lambda:[row['payload'] for row in agent_store.read(session_id,0,1000)
                               if row['event_type']=='question_requested'])
    assert announced,'agent 会话没有产生 question_requested'
    return announced[0]['interaction_id']


# --- T1: legacy still resumes legacy, exactly once -------------------------

def test_a_legacy_question_records_its_answer_and_resumes_nothing(dataset):
    """The answer is history now: the conversation that could have resumed is retired."""
    did=dataset['id'];owner=seed_legacy_turn(did)
    asked=business_questions.ask(did,'主色按什么口径？',['底色主导','整套视觉'])
    assert asked['status']=='pending'
    result=business_questions.answer(did,asked['question_id'],option_ids=['1'])
    assert result['status']=='answered' and result['selected_label']=='底色主导'
    with Session() as s:
        assert s.get(WorkbenchMeta,'chat-followup:'+did) is None
        assert require(s,WorkbenchMeta,'active-chat:'+did).data['turn_id']==owner['turn_id']
    # answering the same way again is idempotent
    again=business_questions.answer(did,asked['question_id'],option_ids=['1'])
    assert again['id']==result['id']
    with Session() as s:assert s.get(WorkbenchMeta,'chat-followup:'+did) is None


# --- T2: an agent answer never reaches the legacy orchestrator -------------

def test_answering_an_agent_interaction_never_resumes_a_legacy_conversation(client,dataset,monkeypatch):
    """Same dataset, both chains alive: the legacy chat has a turn in flight and the agent panel has
    a session. Answering the agent's question must leave the legacy side completely untouched."""
    did=dataset['id']
    owner=seed_legacy_turn(did)
    install_agent_backend(monkeypatch,'ask_interaction')
    session_id=start_agent_session(client,did)
    interaction_id=agent_interaction(client,session_id)
    answered=client.post('/api/agent/sessions/%s/questions/%s/answer'%(session_id,interaction_id),
                         json={'option_id':'a'})
    assert answered.status_code==200 and answered.json()['accepted'] is True
    time.sleep(0.2)
    with Session() as s:
        assert s.get(WorkbenchMeta,'chat-followup:'+did) is None
        active=require(s,WorkbenchMeta,'active-chat:'+did)
        assert active.data['turn_id']==owner['turn_id']
        assert active.data['conversation_id']==owner['conversation_id']
        assert s.get(WorkbenchMeta,'business-question:'+interaction_id) is None


# --- T3: two owners on one dataset stay apart ------------------------------

def test_the_agent_session_owns_only_its_own_interaction(client,dataset,monkeypatch):
    did=dataset['id']
    owner=seed_legacy_turn(did)
    install_agent_backend(monkeypatch,'ask_interaction')
    session_id=start_agent_session(client,did)
    interaction_id=agent_interaction(client,session_id)
    client.post('/api/agent/sessions/%s/questions/%s/cancel'%(session_id,interaction_id))
    time.sleep(0.2)
    with Session() as s:
        assert s.get(WorkbenchMeta,'chat-followup:'+did) is None
        assert require(s,WorkbenchMeta,'active-chat:'+did).data['turn_id']==owner['turn_id']
    settled=[row['payload'] for row in agent_store.read(session_id,0,1000)
             if row['event_type']=='question_settled']
    assert settled[-1]['interaction_id']==interaction_id


# --- T4: wrong owner / stale / repeated answers stay structured errors ----

def test_an_agent_interaction_cannot_be_answered_through_the_legacy_route(client,dataset,monkeypatch):
    did=dataset['id']
    seed_legacy_turn(did)
    install_agent_backend(monkeypatch,'ask_interaction')
    session_id=start_agent_session(client,did)
    interaction_id=agent_interaction(client,session_id)
    response=client.post('/api/datasets/%s/chat/questions/%s/answer'%(did,interaction_id),
                         json={'option_id':'a'})
    assert response.status_code>=400,'legacy 路由不应接受 agent interaction'
    with Session() as s:assert s.get(WorkbenchMeta,'chat-followup:'+did) is None


def test_a_stale_agent_interaction_is_refused_and_queues_nothing(client,dataset,monkeypatch):
    did=dataset['id']
    seed_legacy_turn(did)
    install_agent_backend(monkeypatch,'normal')
    session_id=start_agent_session(client,did)
    response=client.post('/api/agent/sessions/%s/questions/no-such-interaction/answer'%session_id,
                         json={'option_id':'a'})
    assert response.status_code==400
    with Session() as s:assert s.get(WorkbenchMeta,'chat-followup:'+did) is None


# --- T5: the capability boundary itself ------------------------------------

def test_the_agent_runtime_config_withholds_the_business_question(tmp_path):
    """Withheld in the generated project config, not merely discouraged in a prompt."""
    legacy=_runtime_toml('legacy-config-dataset',suffix='chat')
    agent=_runtime_toml('agent-config-dataset',suffix='agent',allow_business_questions=False)
    question='mcp__product_workbench__ask_business_question'
    assert question in _allow(legacy) and question not in _deny(legacy)
    assert _owner_env(legacy)=='chat'
    assert question not in _allow(agent)
    assert question in _deny(agent)
    assert _owner_env(agent)=='none'
    # the rest of the coordinator surface is untouched for both
    for name in ('read_worksheet','read_tagging_rules','route_tagging_skill'):
        assert 'mcp__product_workbench__'+name in _allow(legacy)
        assert 'mcp__product_workbench__'+name in _allow(agent)


def test_the_application_builds_its_agent_runtime_without_the_question_tool(monkeypatch):
    """The wiring, not just the mechanism: what the application actually generates for a session.

    Without this, changing the config generator alone would keep every other test green while the
    agent runtime quietly got the tool back.
    """
    built={}
    original=harness.runtime_dir
    def spy(dataset_id,*args,**kwargs):
        path=original(dataset_id,*args,**kwargs)
        built.update(dataset_id=dataset_id,kwargs=dict(kwargs),path=path)
        return path
    monkeypatch.setattr(harness,'runtime_dir',spy)
    monkeypatch.setattr(harness,'executable',lambda:'reasonix-cli')
    monkeypatch.setattr(agent_application,'environment',lambda *args,**kwargs:{})
    agent_application.backend_factory('wiring-dataset','provider/model')()
    assert built['dataset_id']=='wiring-dataset'
    assert built['kwargs'].get('allow_business_questions') is False
    toml=(built['path']/'reasonix.toml').read_text(encoding='utf-8')
    question='mcp__product_workbench__ask_business_question'
    assert question not in _allow(toml) and question in _deny(toml)
    assert _owner_env(toml)=='none'


def test_the_agent_runtime_is_not_handed_the_business_question_tool():
    """The registration itself: two real MCP servers, one question tool.
    Deliberately not a config-text assertion — this starts the server that the runtime starts."""
    assert 'ask_business_question' in _mcp_tool_names('chat')
    assert 'ask_business_question' not in _mcp_tool_names('none')
    # and the agent's remaining surface is intact rather than accidentally emptied
    agent_tools=_mcp_tool_names('none')
    assert {'read_worksheet','read_tagging_rules','route_tagging_skill'}<=set(agent_tools)


def test_the_agent_chain_has_no_import_path_to_the_legacy_orchestrator():
    """No route through imports or calls: the boundary must not need a runtime guard to hold."""
    for name in AGENT_MODULES:
        source=(ROOT/'backend'/name).read_text(encoding='utf-8')
        imports=[line for line in source.splitlines() if line.startswith(('import ','from '))]
        assert not [line for line in imports if 'business_questions' in line],name
        assert not [line for line in imports if re.search(r'(^|[ .])conversation($|[ .,])',line)],name
        assert '_resume_queued' not in source,name
    application=(ROOT/'backend'/'agent_application.py').read_text(encoding='utf-8')
    assert 'allow_business_questions=False' in application


# --- helpers ---------------------------------------------------------------

def _runtime_toml(dataset_id,**kwargs):
    path=harness.runtime_dir(dataset_id,**kwargs)
    return (path/'reasonix.toml').read_text(encoding='utf-8')


def _allow(toml):
    return json.loads(re.search(r'^allow = (.*)$',toml,re.M).group(1))


def _deny(toml):
    return json.loads(re.search(r'^deny = (.*)$',toml,re.M).group(1))


def _owner_env(toml):
    return re.search(r'"TAG_QUESTION_OWNER" = "([a-z]+)"',toml).group(1)


def _mcp_tool_names(question_owner):
    """Ask a real MCP server, started exactly as the runtime starts it, what it offers."""
    import asyncio
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def collect():
        env=dict(os.environ,TAG_DATASET_ID='ownership-probe',TAG_QUESTION_OWNER=question_owner)
        params=StdioServerParameters(command=sys.executable,args=['-m','backend.mcp_server'],
                                      env=env,cwd=str(ROOT))
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                result=await session.list_tools()
                return sorted(tool.name for tool in result.tools)

    return asyncio.run(collect())


# --- the retired controller stays retired --------------------------------

def test_the_legacy_conversation_controller_is_gone():
    import importlib.util
    assert importlib.util.find_spec('backend.conversation') is None
