"""M6-3B: a session or a run freezes its model, provider and credential, and nothing moves them.

The contract this file pins:

* a launch resolves provider and credential once, at the creation boundary, and passes that explicit
  selection on — never an empty selection that means "whoever is primary right now";
* an unusable credential (missing, disabled, or from another provider) fails before a runtime
  exists, instead of surfacing later as a provider authentication error;
* while this process owns any live workload, every settings mutation that a launch resolves is
  refused, because changing one under a running runtime is what silently moves it;
* one dataset shares one Reasonix home, so a second session on a different provider or model is
  refused rather than allowed to rewrite the configuration the first one is running on.

Everything here is deterministic and offline: a synthetic provider home in a temp directory, no real
credential, no network, and the adapter replaced by a recorder so no process is started.
"""
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import agent_application, harness, jobs, model_settings, runtime_isolation, service
from backend.agent import backend as agent_backend
from backend.agent import reasonix
from backend.app import app
from backend.db import STORAGE, Run, Session, require
from backend.runtime_isolation import CredentialUnavailable, credential_selection
from backend.vision import provider_config
from fake_backend import FakeBackend

REAL_ADAPTER=reasonix.ReasonixAdapter       # captured before any fixture replaces the binding
MODEL='test/text'
OTHER='test/text-b'
DEFS=[{'key':'color','name':'颜色','type':'FACT','definition':'仅提取标题或 SKU 中明确声明的颜色，不确定则留空','options':[],'source':'text'}]


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def providers(tmp_path,monkeypatch):
    """A synthetic provider home: two models, two keys, and one provider that declares no key.

    Written with the same serializer the settings endpoint uses, so the fixture and production agree
    on the file format. No real credential is read, written or reachable from here.
    """
    home=tmp_path/'providers';home.mkdir()
    state={'primary':'cred-1','credentials':[
        {'id':'cred-1','name':'Key One','api_key_env':'K1','enabled':True},
        {'id':'cred-2','name':'Key Two','api_key_env':'K2','enabled':True}]}
    def write():
        credentials=state['credentials']
        primary=next((c for c in credentials if c['id']==state['primary']),None)
        state['provider']={'name':'test','kind':'openai','base_url':'https://test.invalid/v1',
            'models':['text','text-b'],'default':'text','text_model':'text','vision_model':'text',
            'primary_credential_id':state['primary'],
            'credentials':credentials}
        if primary:state['provider']['api_key_env']=primary['api_key_env']
        model_settings._write_config(home/'config.toml',{'default_model':'test/text','providers':[
            state['provider'],
            # A provider whose own schema says it needs no credential at all.
            {'name':'plain','kind':'openai','base_url':'https://plain.invalid/v1','models':['text'],
             'default':'text','text_model':'text','vision_model':'text'}]})
        (home/'.env').write_text('\n'.join('%s="%s-secret"'%(c['api_key_env'],c['id']) for c in credentials)+'\n',
                                 encoding='utf-8')
    write()
    from backend.credential_vault import set_secret
    for credential in state['credentials']:
        set_secret('model', credential['api_key_env'], credential['id']+'-secret')
    monkeypatch.setenv('TAG_PROVIDER_HOME',str(home))
    state['home']=home;state['write']=write
    return state


@pytest.fixture
def recorder(monkeypatch):
    """Every backend a composition builds, plus the environment each launch was given.

    The environment is recorded because that is where the frozen credential becomes a file: seeing
    the selection arrive there is what proves the launch was told which key to use.
    """
    built=[];envs=[];hooks={'on_build':None}
    real_environment=runtime_isolation.environment
    def environment(dataset_id,session_id=None,credential_ids=None):
        env=real_environment(dataset_id,session_id,credential_ids)
        envs.append({'dataset_id':dataset_id,'credential_ids':dict(credential_ids or {})})
        return env
    monkeypatch.setattr(runtime_isolation,'environment',environment)
    monkeypatch.setattr(agent_application,'environment',environment)
    class Recorder(FakeBackend):
        def __init__(self,config):
            super().__init__(reply=('好',));self.config=config;built.append(self)
            hook=hooks['on_build']
            if hook:hook(len(built))
    monkeypatch.setattr(agent_application,'ReasonixAdapter',lambda config:Recorder(config))
    monkeypatch.setattr(reasonix,'ReasonixAdapter',lambda config:Recorder(config))
    return {'built':built,'envs':envs,'hooks':hooks}


@pytest.fixture(autouse=True)
def clean_workloads():
    """The application and the job registry are process state, so one test must not inherit them."""
    agent_application.start()
    jobs.active.clear();jobs.chats.clear()
    yield
    agent_application.start()
    jobs.active.clear();jobs.chats.clear()


@pytest.fixture
def client():
    with TestClient(app) as test_client:yield test_client


def open_run(did,model=MODEL,products=3):
    schema=service.propose(did,'生命周期测试',DEFS);service.approve_schema(schema['id'])
    run=service.create_run(did,schema['id'],'sample',model)
    with Session.begin() as s:
        r=require(s,Run,run['id']);r.product_ids=r.product_ids[:products]
    return run['id']


def dataset_home(did):
    return runtime_isolation._runtime_home(did)


def home_bytes(did):
    home=dataset_home(did)
    return (home/'config.toml').read_bytes()+(home/'.env').read_bytes()


def start_session(did,model=MODEL):
    session=agent_application.create(did,model)
    agent_application.bridge_for(session['id']).start(session['id'])
    return session


# --- T1 model snapshot -----------------------------------------------------

def test_T1_a_session_keeps_the_model_it_was_created_with(dataset,providers,recorder):
    session=start_session(dataset['id'],MODEL)
    adapter=recorder['built'][0]
    assert adapter.config.model==MODEL
    assert adapter.config.runtime_key=='agent:'+dataset['id']
    # The settings the session resolved from move on underneath it.
    providers['primary']='cred-2';providers['write']()
    agent_application.bridge_for(session['id']).send(session['id'],'再问一句')
    assert len(recorder['built'])==1 and recorder['built'][0] is adapter
    assert adapter.config.model==MODEL
    assert session['model']==MODEL                  # the durable row keeps it as well


# --- T2 explicit credential resolution -------------------------------------

def test_T2_a_launch_is_given_an_explicit_credential_not_an_empty_selection(dataset,providers,recorder):
    start_session(dataset['id'])
    assert recorder['envs']==[{'dataset_id':dataset['id'],'credential_ids':{'test':'cred-1'}}]
    # The selection really reached the home the runtime reads.
    secrets_file=(dataset_home(dataset['id'])/'.env').read_text(encoding='utf-8')
    assert 'K1' in secrets_file and 'K2' not in secrets_file


# --- T3 disabled credential ------------------------------------------------

def test_T3_a_disabled_credential_is_refused_before_any_runtime_exists(dataset,providers,recorder):
    providers['credentials'][0]['enabled']=False;providers['write']()
    with pytest.raises(CredentialUnavailable,match='已停用'):
        agent_application.create(dataset['id'],MODEL)
    assert recorder['built']==[] and recorder['envs']==[]
    # An explicit choice of the disabled key is refused too, not silently replaced.
    with pytest.raises(CredentialUnavailable,match='已停用'):
        credential_selection(MODEL,'cred-1')
    # And a launch that was given no selection at all still refuses the disabled primary.
    with pytest.raises(CredentialUnavailable,match='已停用'):
        runtime_isolation.environment(dataset['id'])


# --- T4 no usable credential ----------------------------------------------

def test_T4_a_provider_with_no_usable_credential_fails_before_launch(dataset,providers,recorder):
    providers['credentials']=[];providers['primary']=None;providers['write']()
    with pytest.raises(CredentialUnavailable,match='没有可用的授权'):
        agent_application.create(dataset['id'],MODEL)
    assert recorder['built']==[] and recorder['envs']==[]
    assert service.list_datasets() is not None      # a refused create leaves no half-built session
    # A provider whose own schema declares no credential is not forced to invent one.
    assert credential_selection('plain/text')==('plain',None)


# --- T5 settings lock ------------------------------------------------------

def test_T5_every_settings_mutation_is_refused_while_a_workload_is_live(client,dataset,providers,recorder):
    session=start_session(dataset['id'])
    before=(providers['home']/'config.toml').read_bytes(),(providers['home']/'.env').read_bytes()
    save=client.post('/api/settings/models',json={'name':'test','base_url':'https://other.invalid/v1',
        'text_model':'text','vision_model':'text','api_key':'synthetic'})
    assert save.status_code==409,save.text
    assert save.json()['detail']['code']=='settings_in_use'
    for action in ('delete','disable','primary'):
        answer=client.post('/api/settings/models/test/credentials/cred-2/%s'%action)
        assert answer.status_code==409,answer.text
        assert answer.json()['detail']['code']=='settings_in_use'
    assert ((providers['home']/'config.toml').read_bytes(),(providers['home']/'.env').read_bytes())==before
    assert agent_application.has_active_sessions() is True
    # A live background run holds the same lock, even with no agent session at all.
    agent_application.shutdown()
    jobs.active['run-x']={'stop':False,'client':None}
    answer=client.post('/api/settings/models/test/credentials/cred-2/delete')
    assert answer.status_code==409 and answer.json()['detail']['code']=='settings_in_use'
    jobs.active.pop('run-x',None)
    # So does a batch job, which was already the one guard that existed before this round.
    from backend import batch
    batch.active['job-x']=threading.Event()
    answer=client.post('/api/settings/models/test/credentials/cred-2/delete')
    assert answer.status_code==409 and answer.json()['detail']['code']=='settings_in_use'
    batch.active.pop('job-x',None)


# --- T6 settings unlocked --------------------------------------------------

def test_T6_settings_change_once_the_workload_stopped(dataset,providers,recorder,client):
    session=start_session(dataset['id'])
    agent_application.bridge_for(session['id']).stop(session['id'])
    assert agent_application.has_active_sessions() is False
    answer=client.post('/api/settings/models/test/credentials/cred-2/primary')
    assert answer.status_code==200,answer.text
    recorder['built'].clear();recorder['envs'].clear()
    start_session(dataset['id'])
    assert recorder['envs']==[{'dataset_id':dataset['id'],'credential_ids':{'test':'cred-2'}}]
    assert 'K2' in (dataset_home(dataset['id'])/'.env').read_text(encoding='utf-8')


# --- T7 worker run-level snapshot -----------------------------------------

def test_T7_one_run_resolves_its_credential_once_for_every_product(dataset,providers,recorder,monkeypatch):
    calls=[]
    real=credential_selection
    monkeypatch.setattr(runtime_isolation,'credential_selection',
        lambda model,credential_id=None:(calls.append(model) or real(model,credential_id)))
    run_id=open_run(dataset['id'],MODEL,products=3)
    # The operator changes the primary after the first product: the run must not follow it.
    def after_first(product_index):
        if product_index==1:
            providers['primary']='cred-2';providers['write']()
    recorder['hooks']['on_build']=after_first
    jobs.active[run_id]={'stop':False,'client':None}
    jobs._execute(run_id)
    jobs.active.pop(run_id,None)
    assert calls==[MODEL]                       # resolved once for the whole run, not once per product
    assert len(recorder['built'])==3
    assert {x.config.model for x in recorder['built']}=={MODEL}
    assert [e['credential_ids'] for e in recorder['envs']]==[{'test':'cred-1'}]*3


# --- T8 dangling explicit credential --------------------------------------

def test_T8_an_explicit_credential_that_is_gone_never_falls_back_to_the_primary(dataset,providers):
    with pytest.raises(CredentialUnavailable,match='不存在所选 Key'):
        provider_config(MODEL,'missing-id')
    # No credential named keeps the resolution that already existed.
    endpoint,key,model=provider_config(MODEL)
    assert endpoint=='https://test.invalid/v1/chat/completions' and key=='cred-1-secret' and model=='text'
    # And the launch boundary refuses a credential that belongs to nobody the same way.
    with pytest.raises(CredentialUnavailable,match='不存在所选 Key'):
        credential_selection(MODEL,'missing-id')


# --- T9 same dataset, conflicting runtime configuration -------------------

def test_T9_a_second_session_on_another_model_is_refused_and_leaves_the_home_alone(
        client,dataset,providers,recorder):
    first=start_session(dataset['id'],MODEL)
    before=home_bytes(dataset['id'])
    conflict=client.post('/api/agent/sessions',json={'dataset_id':dataset['id'],'model':OTHER})
    assert conflict.status_code==409,conflict.text
    assert conflict.json()['detail']['code']=='runtime_config_in_use'
    assert len(recorder['built'])==1 and home_bytes(dataset['id'])==before
    # The same frozen configuration is still allowed: it shares the identical launch facts.
    same=client.post('/api/agent/sessions',json={'dataset_id':dataset['id'],'model':MODEL})
    assert same.status_code==200,same.text
    assert same.json()['model']==MODEL
    assert len(recorder['built'])==1 and home_bytes(dataset['id'])==before
    assert first['model']==MODEL


# --- T10 recovery keeps the frozen configuration --------------------------

def test_T10_an_interrupted_runtime_resumes_on_the_frozen_configuration(dataset,providers,recorder,monkeypatch):
    session=start_session(dataset['id'],MODEL)
    frozen=recorder['built'][0].config
    resolved=[]
    monkeypatch.setattr(runtime_isolation,'credential_selection',
                        lambda *a,**k:(resolved.append(a),('test','cred-2'))[1])
    providers['primary']='cred-2';providers['write']()          # the settings move on underneath
    captured={}
    class StopRuntime:
        def __init__(self,executable,args,cwd=None,env=None,**kwargs):
            captured.update(args=args,cwd=cwd,env=env);raise RuntimeError('probe stops before spawn')
    monkeypatch.setattr(reasonix,'Runtime',StopRuntime)
    adapter=REAL_ADAPTER(frozen)
    adapter._needs_resume=True;adapter._remote_session_id='native-session-1'
    with pytest.raises(Exception):adapter._recover_if_needed()
    assert resolved==[]                                          # recovery never re-resolves
    assert captured['args']==['acp','--model',MODEL]
    assert captured['env']==dict(frozen.env)
    assert captured['env']['REASONIX_HOME']==str(dataset_home(dataset['id']))
    assert adapter._config.resume.session_id=='native-session-1'
    assert adapter._config.resume.runtime_key==frozen.runtime_key
    assert session['id']


# --- T11 the earlier contracts are untouched ------------------------------

def test_T11_the_execution_budget_and_backend_contracts_are_unchanged():
    from backend.agent import protocol
    config=reasonix.ReasonixLaunchConfig(executable='x',model='m',cwd='.',env={},runtime_key='k')
    assert config.budget is None and config.resume is None
    assert (agent_application.UI_EXECUTION_BUDGET.first_activity_timeout,
            agent_application.UI_EXECUTION_BUDGET.idle_timeout,
            agent_application.UI_EXECUTION_BUDGET.tool_wait_timeout,
            agent_application.UI_EXECUTION_BUDGET.turn_wall_clock_timeout)==(120,180,360,1200)
    assert (jobs.WORKER_EXECUTION_BUDGET.first_activity_timeout,jobs.WORKER_EXECUTION_BUDGET.idle_timeout,
            jobs.WORKER_EXECUTION_BUDGET.tool_wait_timeout)==(180,300,360)
    assert (jobs.COORDINATOR_EXECUTION_BUDGET.first_activity_timeout,
            jobs.COORDINATOR_EXECUTION_BUDGET.idle_timeout)==(120,240)
    assert reasonix.BUDGET_TRANSPORT_GRACE_SECONDS==60
    assert callable(getattr(reasonix.ReasonixAdapter,'_recover_if_needed'))
    assert callable(getattr(reasonix.ReasonixAdapter,'_try_interrupt'))
    # The backend protocol still declares exactly the capability set the bridge was built against.
    declared={name for name in vars(agent_backend.AgentBackend) if not name.startswith('_')}
    assert declared=={'bind','start','send','cancel','answer_question','cancel_question',
                      'answer_permission','cancel_permission','stop','status'}
    assert callable(getattr(protocol,'line'))
    # Credential selection stays above the adapter; startup diagnostics only redact secret values.
    source=Path(reasonix.__file__).read_text(encoding='utf-8')
    assert 'credential_selection' not in source
    assert 'credential_vault' not in source
    assert 'provider_home' not in source
    assert 'MCP_CALL_TIMEOUT_SECONDS' not in source
    assert harness.MCP_CALL_TIMEOUT_SECONDS==300
