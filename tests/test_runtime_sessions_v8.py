from types import SimpleNamespace
from sqlalchemy import select
from backend import chat_threads,conversation
from backend.db import Session,AgentRuntimeSession,Message,WorkbenchMeta,uid
from backend.harness import runtime_dir
from backend.harness import HarnessError


class FakeACP:
    created=[]
    def __init__(self,dataset_id,model,on_event=None,session_id=None,conversation_id=None,vision_model=None,credential_id=None,vision_credential_id=None,role=None,**_):
        self.dataset_id=dataset_id;self.model=model;self.vision_model=vision_model;self.credential_id=credential_id;self.vision_credential_id=vision_credential_id
        self.on_event=on_event;self.closed=False;self.proc=SimpleNamespace(poll=lambda:None);self.session_id=session_id or f'native-{len(self.created)+1}';self.prompts=[]
        self.created.append(self)
    def prompt(self,text):self.prompts.append(text);self.on_event({'type':'text','text':'完成'})
    def close(self):self.closed=True


def assistant_turn(did,cid,label):
    turn=uid()
    with Session.begin() as session:
        session.add(WorkbenchMeta(id='chat-turn:'+turn,dataset_id=did,kind='chat_turn',data={'turn_id':turn,'conversation_id':cid}))
        message=Message(dataset_id=did,turn_id=turn,role='assistant',text='',status='running',events=[],presentation={});session.add(message);session.flush()
        session.merge(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',data={'turn_id':turn,'message_id':message.id,'conversation_id':cid}))
        ident=message.id
    conversation.run(did,ident,turn,label,'provider/'+label,cid,'provider/vision','key','vision-key')


def test_a_b_a_model_switch_creates_three_native_sessions(dataset,monkeypatch):
    did=dataset['id'];cid=chat_threads.create(did,'测试')['id'];FakeACP.created=[];conversation.clients.clear();monkeypatch.setattr(conversation,'ACP',FakeACP)
    assistant_turn(did,cid,'A');assistant_turn(did,cid,'B');assistant_turn(did,cid,'A')
    with Session() as session:runtimes=list(session.scalars(select(AgentRuntimeSession).where(AgentRuntimeSession.conversation_id==cid).order_by(AgentRuntimeSession.created_at)))
    assert [x.native_session_id for x in runtimes]==['native-1','native-2','native-3']
    assert [x.status for x in runtimes]==['parked','parked','active']
    assert '当前对话上下文' in FakeACP.created[1].prompts[0]
    conversation.clients.clear()


def test_switching_frontend_conversations_resumes_each_own_native_session(dataset,monkeypatch):
    did=dataset['id'];first=chat_threads.create(did,'一')['id'];second=chat_threads.create(did,'二')['id'];FakeACP.created=[];conversation.clients.clear();monkeypatch.setattr(conversation,'ACP',FakeACP)
    assistant_turn(did,first,'A');assistant_turn(did,second,'A');assistant_turn(did,first,'A')
    assert [x.session_id for x in FakeACP.created]==['native-1','native-2','native-1']
    with Session() as session:
        one=list(session.scalars(select(AgentRuntimeSession).where(AgentRuntimeSession.conversation_id==first)))
        two=list(session.scalars(select(AgentRuntimeSession).where(AgentRuntimeSession.conversation_id==second)))
    assert len(one)==len(two)==1 and one[0].status=='active' and two[0].status=='suspended'
    conversation.clients.clear()


def test_runtime_role_controls_prompt_identity_and_worker_environment(dataset):
    did=dataset['id']
    coordinator=runtime_dir(did,'chat-explicit',conversation_id='conversation-one',role='coordinator')
    coordinator_config=(coordinator/'reasonix.toml').read_text(encoding='utf-8')
    coordinator_prompt=(coordinator/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'TAG_CONVERSATION_ID' in coordinator_config and 'TAG_RUN_ID' not in coordinator_config and 'TAG_PRODUCT_ID' not in coordinator_config
    assert '持续对话协调员' in coordinator_prompt and '"bash"' in coordinator_config and '"history"' in coordinator_config
    worker=runtime_dir(did,'run-explicit','product-one',role='worker')
    worker_config=(worker/'reasonix.toml').read_text(encoding='utf-8')
    worker_prompt=(worker/'PRODUCT_ANALYST_AGENT.md').read_text(encoding='utf-8')
    assert 'TAG_RUN_ID' in worker_config and 'TAG_PRODUCT_ID' in worker_config
    assert '执行打标' in worker_prompt


def test_workspace_connection_failure_rebuilds_native_session_once(dataset,monkeypatch):
    class RecoveringACP(FakeACP):
        def prompt(self,text):
            self.prompts.append(text)
            if self.session_id=='native-1':
                self.on_event({'type':'text','text':'product_workbench 本会话不可用'})
                raise HarnessError('workspace_connection_error')
            self.on_event({'type':'text','text':'恢复完成'})
    did=dataset['id'];cid=chat_threads.create(did,'恢复测试')['id'];FakeACP.created=[];conversation.clients.clear();monkeypatch.setattr(conversation,'ACP',RecoveringACP)
    assistant_turn(did,cid,'A')
    with Session() as session:
        runtimes=list(session.scalars(select(AgentRuntimeSession).where(AgentRuntimeSession.conversation_id==cid).order_by(AgentRuntimeSession.created_at)))
        message=session.scalar(select(Message).where(Message.text=='恢复完成'))
    assert len(runtimes)==2 and runtimes[0].status=='parked' and runtimes[0].invalid_reason=='workspace_connection_failed'
    assert runtimes[1].status=='active' and message.status=='completed'
    conversation.clients.clear()
