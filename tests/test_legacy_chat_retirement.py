"""M5-4: the legacy chat runtime is retired, and everything around it is not.

Three things must hold at once and they pull in opposite directions: the HTTP surface that could
start a legacy Agent turn is gone, the conversation history it produced is still readable, and the
batch pipeline that shares the same runtime layer still works. Each is asserted here from the
running application rather than from a module's absence.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import chat_threads, draft, harness, jobs
from backend.app import app
from backend.db import STORAGE,AgentRuntimeSession,ChatQuestion,Message,Session,WorkbenchMeta,require,uid


@pytest.fixture
def client():
    with TestClient(app) as test_client:yield test_client


def route_paths():
    paths=[]
    for route in app.routes:
        path=getattr(route,'path',None)
        if path:paths.append(path)
        # an included router is one entry until it is resolved; ask it the same question
        for inner in getattr(route,'routes',[]) or []:paths.append(getattr(inner,'path',''))
    return paths


# --- the command surface is gone -------------------------------------------

def test_the_legacy_chat_command_routes_are_not_registered():
    """Retired, not quiesced: there is no HTTP entry point left to call them on."""
    for path in ('/api/datasets/{dataset_id}/chat','/api/datasets/{dataset_id}/chat/stop',
                 '/api/datasets/{dataset_id}/chat/questions/{question_id}/answer',
                 '/api/drafts/{did}/runtime'):
        assert path not in route_paths(),path


def test_the_legacy_conversation_controller_is_gone():
    assert importlib.util.find_spec('backend.conversation') is None
    source=(Path(__file__).resolve().parents[1]/'backend'/'app.py').read_text(encoding='utf-8')
    assert 'conversation.' not in source,'app must not reach a retired controller'


# --- the history and the business surface remain ---------------------------

def test_the_history_apis_still_work(client,dataset):
    did=dataset['id']
    thread=chat_threads.create(did,'退役后的历史')
    listing=client.get('/api/datasets/%s/conversations'%did)
    assert listing.status_code==200 and [x for x in listing.json() if x['id']==thread['id']]
    renamed=client.post('/api/datasets/%s/conversations/%s'%(did,thread['id']),json={'name':'改名后'})
    assert renamed.status_code==200 and renamed.json()['name']=='改名后'
    recycled=client.post('/api/datasets/%s/conversations/%s'%(did,thread['id']),json={'deleted':True})
    assert recycled.status_code==200 and recycled.json()['deleted'] is True
    restored=client.post('/api/datasets/%s/conversations/%s'%(did,thread['id']),json={'deleted':False})
    assert restored.status_code==200 and restored.json()['deleted'] is False
    client.post('/api/datasets/%s/conversations/%s'%(did,thread['id']),json={'deleted':True})
    assert client.delete('/api/datasets/%s/conversations/%s'%(did,thread['id'])).status_code==200


def test_history_loads_with_its_messages_questions_and_presentation(client,dataset):
    """The draft payload is what the viewer reads; it must carry the recorded history unchanged."""
    did=dataset['id']
    thread=chat_threads.create(did,'历史查看')
    turn=uid()
    with Session.begin() as s:
        s.add(WorkbenchMeta(id='chat-turn:'+turn,dataset_id=did,kind='chat_turn',
                            data={'turn_id':turn,'conversation_id':thread['id']}))
        message=Message(id=uid(),dataset_id=did,turn_id=turn,role='assistant',status='completed',
                        text='历史回答',presentation={'heading':'历史标题','discussion':'历史回答'},
                        events=[{'type':'tool','title':'读取工作表','status':'completed'}])
        s.add(message);s.flush()
        s.add(ChatQuestion(id=uid(),dataset_id=did,turn_id=turn,message_id=message.id,
                           prompt='历史问题',options=[{'option_id':'1','label':'甲'}],status='pending'))
        message_id=message.id
    payload=client.get('/api/drafts/%s'%did).json()
    stored=next(m for m in payload['messages'] if m['id']==message_id)
    assert stored['text']=='历史回答'
    assert stored['presentation']['heading']=='历史标题'
    assert stored['conversation_id']==thread['id']        # decorated onto the thread
    assert [q['prompt'] for q in payload['questions']]==['历史问题']


# --- the batch pipeline that shares the runtime layer is untouched ---------

def test_the_batch_pipeline_is_still_a_separate_chain_of_its_own():
    """The tagging pipeline is a separate production chain and must survive this retirement.

    M5-5 moved its runtimes onto the backend contract; what must not change is that it still builds
    its own worker and text-facts runtimes and never reaches a conversation controller.
    """
    assert 'ACP' not in vars(jobs),'the batch chain must not build runtimes through harness.ACP'
    root=Path(__file__).resolve().parents[1]/'backend'
    text=(root/'jobs.py').read_text(encoding='utf-8')
    # the worker runtime is how a product is actually tagged; losing this call loses tagging
    assert re.search(r"client=_backend\(dataset_id,model,run_id,pid,role='worker'",text),'worker runtime'
    assert re.search(r"client=_backend\(dataset_id,model,'chat',role='coordinator'",text),'text-facts runtime'
    for source in ('jobs.py','batch.py','text_facts.py'):
        body=(root/source).read_text(encoding='utf-8')
        assert not re.search(r'conversation\.|import .*conversation',body),source

def test_batch_runtimes_declare_that_they_are_not_the_question_owner():
    """Every production runtime that builds a runtime dir says so explicitly, so no runtime relies on
    the compatibility default and inherits a capability it could never deliver on."""
    root=Path(__file__).resolve().parents[1]/'backend'
    for path in sorted(root.glob('*.py')):
        if path.name in ('harness.py','agent_application.py'):continue      # the definitions
        source=path.read_text(encoding='utf-8')
        for match in re.finditer(r'(?:runtime_dir|ACP)\(',source):
            tail=source[match.end():source.index(')',match.end())]
            assert 'allow_business_questions=False' in tail,'%s: %s'%(path.name,match.group(0))
    # and the two chains that do build one agree with the config generator
    assert 'allow_business_questions=False' in (root/'agent_application.py').read_text(encoding='utf-8')
    assert 'allow_business_questions=False' in (root/'jobs.py').read_text(encoding='utf-8')


def test_the_legacy_question_tool_is_withheld_from_every_production_runtime(dataset):
    """Two real MCP servers, started the way the production runtimes start them."""
    assert 'ask_business_question' not in _mcp_tools('none')
    for suffix,product,role in (('run-x','product-x','worker'),('chat',None,'coordinator')):
        config=_runtime_config(dataset['id'],suffix,product,role=role)
        allow=json.loads(re.search(r'^allow = (.*)$',config,re.M).group(1))
        assert 'mcp__product_workbench__ask_business_question' not in allow,(suffix,role)
        assert 'TAG_QUESTION_OWNER" = "none' in config,(suffix,role)


def _runtime_config(dataset_id,suffix,product_id=None,role='coordinator'):
    path=harness.runtime_dir(dataset_id,suffix,product_id,role=role,allow_business_questions=False)
    return (path/'reasonix.toml').read_text(encoding='utf-8')


def _mcp_tools(question_owner):
    import asyncio,os,sys
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    root=Path(__file__).resolve().parents[1]

    async def collect():
        env=dict(os.environ,TAG_DATASET_ID='retirement-probe',TAG_QUESTION_OWNER=question_owner)
        params=StdioServerParameters(command=sys.executable,args=['-m','backend.mcp_server'],env=env,cwd=str(root))
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                return sorted(tool.name for tool in (await session.list_tools()).tools)

    return asyncio.run(collect())


def test_destroying_a_legacy_conversation_cleans_its_native_session_residue(dataset):
    """The runtime binding is retired, but an old conversation's native session files are recorded
    nowhere else. Destroying the conversation is the last place that can still remove them, and it
    must remove only the ones it knows belong to that conversation."""
    did=dataset['id']
    thread=chat_threads.create(did,'待彻底删除')
    sessions=STORAGE/'datasets'/did/'reasonix'/'sessions';sessions.mkdir(parents=True,exist_ok=True)
    mine=sessions/'legacy-mine.session';mine.mkdir()
    (mine/'state.json').write_text('{}',encoding='utf-8')
    mine_file=sessions/'legacy-mine.meta'
    mine_file.write_text('{}',encoding='utf-8')
    other=sessions/'someone-else.session';other.mkdir()
    (other/'state.json').write_text('{}',encoding='utf-8')
    with Session.begin() as s:
        s.add(AgentRuntimeSession(dataset_id=did,conversation_id=thread['id'],native_session_id='legacy-mine',
                                  role='coordinator',model='provider/model',fingerprint='fp',status='invalid',
                                  invalid_reason='legacy_profile_or_cwd'))
    chat_threads.update(did,thread['id'],{'deleted':True})
    chat_threads.destroy(did,thread['id'])
    with Session() as s:
        assert s.get(WorkbenchMeta,thread['id']) is None
        assert list(s.scalars(select(AgentRuntimeSession).where(AgentRuntimeSession.conversation_id==thread['id'])))==[]
    assert not mine.exists() and not mine_file.exists(),'known residue must be removed'
    assert other.is_dir() and (other/'state.json').is_file(),'an unrelated session must survive'
