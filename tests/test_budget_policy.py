"""M6-2B: the production compositions inject their own budget, and HTTP names the reason.

The policy is caller-owned: these tests capture the launch facts each composition actually builds, so
a budget can only come from the call site — never from a role string inside the adapter.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import agent_application, harness, jobs, runtime_isolation, service
from backend.agent import reasonix
from backend.agent.backend import TurnError
from backend.agent.reasonix import ExecutionBudget
from backend.app import app
from backend.db import Message, Run, Session, STORAGE, require, uid
from fake_backend import FakeBackend

DEFS=[{'key':'color','name':'颜色','type':'FACT','definition':'仅提取标题或 SKU 中明确声明的颜色，不确定则留空','options':[],'source':'text'}]
UI=(120,180,360,1200)
WORKER=(180,300,360,1200)
COORDINATOR=(120,240,360,1200)


def values(budget):
    return (budget.first_activity_timeout,budget.idle_timeout,budget.tool_wait_timeout,
            budget.turn_wall_clock_timeout)


@pytest.fixture
def capture(monkeypatch):
    """Record the launch config of every backend a composition builds."""
    built=[]
    state={'send':None}
    class Recorder:
        def __init__(self,config):
            self.config=config;built.append(self)
        def bind(self,sink):self.sink=sink
        def start(self):pass
        def send(self,text):
            hook=state['send']
            if hook:hook(self,text)
        def cancel(self):return True
        def stop(self):pass
        def status(self):return {'alive':True}
    monkeypatch.setattr(reasonix,'ReasonixAdapter',Recorder)
    monkeypatch.setattr(runtime_isolation,'environment',lambda *a,**k:{})
    monkeypatch.setattr(runtime_isolation,'credential_selection',lambda *a,**k:('test','cred-1'))
    return built,state


# --- policies --------------------------------------------------------------

def test_T1_the_agent_ui_composition_injects_its_own_budget(capture,monkeypatch):
    built,state=capture
    monkeypatch.setattr(agent_application,'ReasonixAdapter',
                        lambda config:(built.append(type('R',(),{'config':config})()) or built[-1]))
    adapter=agent_application.backend_factory('ds-policy','provider/model')()
    assert values(adapter.config.budget)==UI
    assert adapter.config.runtime_key=='agent:ds-policy'


def test_T2_the_worker_composition_injects_the_worker_budget(capture,dataset):
    """The worker call site decides, so this drives the real per-product loop."""
    built,state=capture
    run=open_run(dataset)
    with Session.begin() as s:
        r=require(s,Run,run['id']);r.product_ids=r.product_ids[:2]
    jobs.active[run['id']]={'stop':False,'client':None}
    jobs._execute(run['id'])
    assert len(built)==2
    assert [values(adapter.config.budget) for adapter in built]==[WORKER,WORKER]


def test_T3_the_coordinator_composition_injects_the_coordinator_budget(capture,dataset):
    """The coordinator call site decides, so this drives the real chat path."""
    built,state=capture
    did=dataset['id'];ident=uid()
    with Session.begin() as s:
        s.add(Message(id=ident,dataset_id=did,role='assistant',text='',status='running'))
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'看图片','test/model')
    assert len(built)==1 and values(built[0].config.budget)==COORDINATOR


def test_T4_the_budget_comes_from_the_caller_not_from_the_role(capture):
    """The same role twice, two different budgets: nothing inside can be deriving one from the other."""
    built,state=capture
    first=jobs._backend('ds-policy','provider/model','run-a','product-a',role='worker',
                        budget=ExecutionBudget(11,12,13,14))
    second=jobs._backend('ds-policy','provider/model','run-b','product-b',role='worker',
                         budget=ExecutionBudget(21,22,23,24))
    assert values(first.config.budget)==(11,12,13,14)
    assert values(second.config.budget)==(21,22,23,24)
    source=Path(reasonix.__file__).read_text(encoding='utf-8')
    assert 'WORKER_EXECUTION_BUDGET' not in source and 'COORDINATOR_EXECUTION_BUDGET' not in source
    assert 'if role' not in source.replace('if role not in',''),'the adapter must not select a policy by role'


def test_the_default_budget_stays_optional(capture):
    built,state=capture
    adapter=jobs._backend('ds-policy','provider/model','run-none','product-none')
    assert adapter.config.budget is None


def test_the_transport_ceiling_is_derived_from_the_wall_clock_budget():
    """1200 semantic + the M6-2A supervision grace; nothing here hardcodes 1260."""
    assert UI[3]+reasonix.BUDGET_TRANSPORT_GRACE_SECONDS==1260


def test_the_mcp_call_bound_is_still_its_own_layer(dataset):
    """360s supervises the tool; the peer still enforces its own 300s per MCP call."""
    path=harness.runtime_dir(dataset['id'],'budget-layer',role='coordinator')
    assert 'call_timeout_seconds = 300' in (path/'reasonix.toml').read_text(encoding='utf-8')
    assert 300 < UI[2]


# --- HTTP projection -------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(app) as test_client:yield test_client


def install(monkeypatch,error):
    def factory(dataset_id,model,credentials=None):
        def build():
            return FakeBackend(mode='normal')
        return build
    monkeypatch.setattr(agent_application,'backend_factory',factory)
    monkeypatch.setattr(agent_application,'credential_selection',lambda *a,**k:('test','cred-1'))
    monkeypatch.setattr(agent_application,'_BRIDGES',{})
    monkeypatch.setattr(agent_application,'_SESSIONS',{})
    monkeypatch.setattr(agent_application,'_IDENTITIES',{})
    monkeypatch.setattr(FakeBackend,'send',lambda self,text:(_ for _ in ()).throw(error))


def test_T5_http_names_the_structured_timeout_reason(client,dataset,monkeypatch):
    install(monkeypatch,TurnError('回合超过预算已放弃',code='turn_idle_timeout'))
    created=client.post('/api/agent/sessions',json={'dataset_id':dataset['id'],'model':'m'})
    session_id=created.json()['id']
    assert client.post('/api/agent/sessions/%s/start'%session_id).status_code==200
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'你好'})
    assert response.status_code==502
    detail=response.json()['detail']
    assert detail['code']=='turn_error' and detail['reason']=='turn_idle_timeout'


def test_T6_an_ordinary_turn_error_keeps_the_old_shape(client,dataset,monkeypatch):
    install(monkeypatch,TurnError('普通失败'))
    created=client.post('/api/agent/sessions',json={'dataset_id':dataset['id'],'model':'m'})
    session_id=created.json()['id']
    assert client.post('/api/agent/sessions/%s/start'%session_id).status_code==200
    response=client.post('/api/agent/sessions/%s/messages'%session_id,json={'text':'你好'})
    assert response.status_code==502
    detail=response.json()['detail']
    assert detail['code']=='turn_error' and 'reason' not in detail


# --- jobs keep their existing failure semantics ----------------------------

def open_run(dataset):
    schema=service.propose(dataset['id'],'策略测试',DEFS);service.approve_schema(schema['id'])
    return service.create_run(dataset['id'],schema['id'],'sample','测试')


def test_T7_a_worker_semantic_timeout_uses_the_existing_failure_path(capture,dataset):
    built,state=capture
    run=open_run(dataset)
    with Session.begin() as s:
        r=require(s,Run,run['id']);r.product_ids=r.product_ids[:2]
    def timeout(adapter,text):
        raise TurnError('回合超过预算已放弃',code='turn_idle_timeout')
    state['send']=timeout
    jobs.active[run['id']]={'stop':False,'client':None}
    jobs._execute(run['id'])
    with Session() as s:
        r=require(s,Run,run['id'])
        assert r.status=='partial'                       # an ordinary failure, not a crash or a cancel
        assert set(r.errors.values())=={'回合超过预算已放弃'}
    assert run['id'] not in jobs.active
    assert len(built)==2                                 # the loop carried on to the next product


def test_T8_a_coordinator_semantic_timeout_uses_the_existing_failure_path(capture,dataset):
    built,state=capture
    did=dataset['id'];ident=uid()
    with Session.begin() as s:
        s.add(Message(id=ident,dataset_id=did,role='assistant',text='',status='running'))
    state['send']=lambda adapter,text:(_ for _ in ()).throw(TurnError('回合超过预算已放弃',code='turn_wall_clock_timeout'))
    jobs.chats[did]={'client':None,'stop':False}
    jobs._chat(did,ident,'看图片','test/model')
    with Session() as s:
        message=require(s,Message,ident)
        assert message.status=='failed' and '回合超过预算已放弃' in message.text
    assert jobs.chats=={}
