"""M5-6B: the persisted business-question capability no longer exists in any runtime.

A persisted question used to belong to the runtime that asked it, and only that runtime could
register the tool. The capability itself is gone: the agent clarifies through an interaction on its
own session, background work does not ask at all, and no generated configuration offers the tool to
anyone. What remains is the boundary these tests defend — an agent answer must never reach the
retired legacy orchestration, and no configuration drift may hand the capability back.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import agent_application,chat_threads,harness
from backend.agent import events as agent_store
from backend.app import app
from backend.db import Message,Session,WorkbenchMeta,require,uid
from fake_backend import FakeBackend

ROOT=Path(__file__).resolve().parents[1]
AGENT_MODULES=['agent/backend.py','agent/bridge.py','agent/events.py','agent/protocol.py','agent/reasonix.py',
               'agent/runtime.py','agent/stream.py','agent_application.py','agent_http.py']
QUESTION_TOOL='mcp__product_workbench__ask_business_question'


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
    """A legacy chat turn in flight: the owner marker a business question used to attach to."""
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


# --- T5: the capability is absent, not merely withheld --------------------

def test_no_generated_runtime_configuration_mentions_the_question_tool():
    """Absent from everything a runtime is actually handed: the project config and its instructions.

    The instructions are generated per runtime, so this protects the production surface the peer
    reads — not the historical documents that still describe the retired capability.
    """
    for dataset_id,suffix,role in (('question-config-a','chat','coordinator'),
                                   ('question-config-b','run-x','worker'),
                                   ('question-config-c','agent','coordinator')):
        product='product-x' if role=='worker' else None
        path=harness.runtime_dir(dataset_id,suffix,product,role=role)
        toml=(path/'reasonix.toml').read_text(encoding='utf-8')
        instructions=(path/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
        assert QUESTION_TOOL not in toml,(dataset_id,role)
        assert 'ask_business_question' not in toml,(dataset_id,role)
        assert 'TAG_QUESTION_OWNER' not in toml,(dataset_id,role)
        assert 'ask_business_question' not in instructions,(dataset_id,role)
        # the rest of the surface is intact rather than accidentally emptied
        for name in ('read_worksheet','read_tagging_rules','route_tagging_skill'):
            assert 'mcp__product_workbench__'+name in _allow(toml),(dataset_id,role)
        assert QUESTION_TOOL not in _allow(toml) and QUESTION_TOOL not in _deny(toml),(dataset_id,role)
    # the coordinator is told which interaction it may use instead, so the instruction is not blank
    coordinator=harness.runtime_dir('question-config-d','chat',role='coordinator')
    assert '原生 ask' in (coordinator/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')


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
    assert 'allow_business_questions' not in built['kwargs']
    toml=(built['path']/'reasonix.toml').read_text(encoding='utf-8')
    assert QUESTION_TOOL not in toml and 'ask_business_question' not in toml


def test_no_runtime_configuration_generator_can_reintroduce_the_tool():
    """The switch itself is gone: nothing accepts a request for the capability any more.

    The system prompt still names the old tool in prose; rewriting prompt text is outside this
    slice, so this asserts the switch and the generated lists instead of a whole-file text match.
    """
    source=(ROOT/'backend'/'harness.py').read_text(encoding='utf-8')
    assert 'allow_business_questions' not in source
    assert 'TAG_QUESTION_OWNER' not in source
    toml=_runtime_toml('guard-dataset','chat',role='coordinator')
    assert QUESTION_TOOL not in _allow(toml)
    assert QUESTION_TOOL not in _deny(toml)


def test_the_agent_runtime_is_not_handed_the_business_question_tool():
    """The registration itself: a real MCP server, started exactly as a runtime starts it.

    Deliberately not a config-text assertion — this starts the server the runtime starts, and a
    server that registered the tool would say so here.
    """
    tools=_mcp_tool_names()
    assert 'ask_business_question' not in tools
    # and the remaining surface is intact rather than accidentally emptied
    assert {'read_worksheet','read_tagging_rules','route_tagging_skill'}<=set(tools)


def test_the_business_question_capability_is_gone_from_the_backend():
    """The execution path itself: no module claims the capability any more."""
    import importlib.util
    assert importlib.util.find_spec('backend.business_questions') is None
    assert 'business_questions' not in (ROOT/'backend'/'mcp_server.py').read_text(encoding='utf-8')


def test_the_agent_chain_has_no_import_path_to_the_legacy_orchestrator():
    """No route through imports or calls: the boundary must not need a runtime guard to hold."""
    for name in AGENT_MODULES:
        source=(ROOT/'backend'/name).read_text(encoding='utf-8')
        imports=[line for line in source.splitlines() if line.startswith(('import ','from '))]
        assert not [line for line in imports if 'business_questions' in line],name
        assert not [line for line in imports if re.search(r'(^|[ .])conversation($|[ .,])',line)],name
        assert '_resume_queued' not in source,name


# --- helpers ---------------------------------------------------------------

def _runtime_toml(dataset_id,suffix,product_id=None,**kwargs):
    path=harness.runtime_dir(dataset_id,suffix,product_id,**kwargs)
    return (path/'reasonix.toml').read_text(encoding='utf-8')


def _allow(toml):
    return json.loads(re.search(r'^allow = (.*)$',toml,re.M).group(1))


def _deny(toml):
    return json.loads(re.search(r'^deny = (.*)$',toml,re.M).group(1))


def _mcp_tool_names():
    """Ask a real MCP server, started exactly as the runtime starts it, what it offers."""
    import asyncio
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def collect():
        env=dict(os.environ,TAG_DATASET_ID='ownership-probe')
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
