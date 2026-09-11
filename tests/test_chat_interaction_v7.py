import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend.db import Session,Message,ChatQuestion,WorkbenchMeta,BatchJob,require,uid
from backend import conversation,draft,jobs
from backend.harness import ACP,HarnessError,_redact_text
from backend.conversation_presentation import build

def definition(key,name=None,depends_on=None):
    return {'key':key,'name':name or key,'type':'FACT','definition':'从商品文字中提取明确值','options':[],'source':'text',**({'depends_on':depends_on} if depends_on else {})}

def add_column(did,key,name=None,depends_on=None):
    proposal=draft.propose(did,'新增',[{'kind':'add','definition':definition(key,name,depends_on)}])
    draft.confirm(did,proposal['id'])
    return next(c for c in draft.sheet(did)['columns'] if c['key']==key)

def fake_acp():
    instance=object.__new__(ACP)
    instance.question_count=0;instance.pending_questions={};instance.question_lock=__import__('threading').Lock()
    instance.current_issue=None;instance.last_tool_failure=None;instance.user_cancelled=False;instance.ask_seen=False
    instance.diagnostics=[];instance.model='provider/model';instance.session_id='session-test';instance.proc=SimpleNamespace(poll=lambda:None)
    return instance

def permission(raw,multi=False):
    raw={**raw,'multiSelect':multi}
    return {'id':41,'params':{'toolCall':{'toolCallId':'ask-example','kind':'other','rawInput':raw},'options':[
        {'optionId':raw['id']+':1','kind':'allow_once'},{'optionId':raw['id']+':2','kind':'allow_once'},
        {'optionId':'reject','kind':'reject_once'}]}}

def test_business_ask_accepts_only_structured_single_choice():
    client=fake_acp();raw={'id':'color-mode','question':'主色按什么口径？','options':[{'label':'底色主导','description':'按最大面积判断'},{'label':'整套视觉','description':'考虑上下装'}]}
    question=client._business_question(permission(raw))
    assert question['type']=='question' and question['question_key']=='color-mode'
    assert [x['option_id'] for x in question['options']]==['color-mode:1','color-mode:2']
    assert client._business_question(permission(raw,multi=True))['error'].startswith('当前网页暂不支持多选')
    invalid=permission(raw);invalid['params']['toolCall']['toolCallId']='bash-1'
    assert client._business_question(invalid) is None

def test_reasonix_125_uppercase_option_keys_are_supported():
    client=fake_acp();raw={'id':'q1','question':'尺码段如何分？','multi':False,'options':[{'Label':'固定边界','Description':'跨批次可比'},{'Label':'按批次等分','Description':'每批动态计算'}]}
    question=client._business_question(permission(raw))
    assert [item['label'] for item in question['options']]==['固定边界','按批次等分']

@pytest.mark.parametrize(('detail','ask_seen','code'),[
    ('HTTP/1.1 200 OK',True,'interaction_bridge_error'),
    ('HTTP/1.1 200 OK',False,'unknown_acp_error'),
    ('HTTP/1.1 401 Unauthorized',False,'provider_auth_error'),
    ('HTTP/1.1 404 Not Found',False,'provider_model_error'),
    ('HTTP 400 model_not_found',False,'provider_model_error'),
    ('connection timed out',False,'provider_network_error'),
])
def test_error_classification_uses_real_evidence(detail,ask_seen,code):
    client=fake_acp();client.ask_seen=ask_seen
    assert client._classify_error('error',detail)[0]==code

def test_permission_denial_reports_tool_name():
    client=fake_acp();client.current_issue={'code':'tool_permission_denied','message':'Reasonix 尝试调用工作台未开放的工具“bash”，已阻止。'}
    code,message=client._classify_error('error',None)
    assert code=='tool_permission_denied' and 'bash' in message


def test_user_cancel_wins_even_if_reasonix_returns_end_turn():
    client=fake_acp()
    def cancelled_rpc(*_args,**_kwargs):
        client.user_cancelled=True
        return {'stopReason':'end_turn'}
    client.rpc=cancelled_rpc
    with pytest.raises(HarnessError) as error:client.prompt('测试停止')
    assert error.value.code=='interaction_cancelled'


def test_technical_redaction_hides_credentials():
    redacted=_redact_text('Authorization: Bearer abc123 api_key=secret-value token:opaque')
    assert 'abc123' not in redacted and 'secret-value' not in redacted and 'opaque' not in redacted

class BridgeClient:
    closed=False
    proc=SimpleNamespace(poll=lambda:None)
    def __init__(self):self.answers=[]
    def answer_question(self,qid,option_id):self.answers.append((qid,option_id))

def test_answer_question_continues_bridge_without_confirming_or_creating_jobs(dataset):
    did=dataset['id'];mid=uid();qid=uid();client=BridgeClient();conversation.clients[did]=client
    try:
        with Session.begin() as session:
            session.add(Message(id=mid,dataset_id=did,turn_id='turn-one',role='assistant',text='请选择',status='running'));session.flush()
            session.add(ChatQuestion(id=qid,dataset_id=did,turn_id='turn-one',message_id=mid,prompt='选一个',options=[{'option_id':'one:1','label':'甲'},{'option_id':'one:2','label':'乙'}]))
        result=conversation.answer(did,qid,'one:2')
        assert result['status']=='answered' and result['selected_label']=='乙'
        assert client.answers==[(qid,'one:2')]
        with Session() as session:assert session.scalar(select(BatchJob.id)) is None
    finally:conversation.clients.pop(did,None)

def test_restart_interrupts_pending_questions_and_active_turn(dataset):
    did=dataset['id'];mid=uid();qid=uid()
    with Session.begin() as session:
        session.add(Message(id=mid,dataset_id=did,turn_id='turn-two',role='assistant',text='',status='running'));session.flush()
        session.add(ChatQuestion(id=qid,dataset_id=did,turn_id='turn-two',message_id=mid,prompt='选一个',options=[]))
        session.add(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',data={'turn_id':'turn-two'}))
    jobs.recover()
    with Session() as session:
        assert require(session,Message,mid).status=='interrupted'
        assert require(session,ChatQuestion,qid).status=='interrupted'
        assert session.get(WorkbenchMeta,'active-chat:'+did) is None

def test_proposal_group_deduplicates_merges_and_supersedes(dataset):
    did=dataset['id'];a=add_column(did,'a');b=add_column(did,'b');pids=[row['id'] for row in draft.sheet(did)['rows'][:2]]
    with Session.begin() as session:session.add(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',data={'turn_id':'turn-a'}))
    first=draft.propose(did,'第一列',[{'kind':'execute','column_ids':[a['id']],'product_ids':[pids[0]]}],use_active_turn=True)
    duplicate=draft.propose(did,'重复',[{'kind':'execute','column_ids':[a['id']],'product_ids':[pids[0]]}],use_active_turn=True)
    assert duplicate['id']==first['id'] and duplicate['merge_action']=='reused'
    combined=draft.propose(did,'第二列',[{'kind':'execute','column_ids':[b['id']],'product_ids':[pids[1]]}],use_active_turn=True)
    assert combined['group_id']==first['group_id'] and combined['group_revision']==2
    assert first['id']!=combined['id'] and len(combined['changes'][0]['column_ids'])==2
    with Session.begin() as session:require(session,WorkbenchMeta,'active-chat:'+did).data={'turn_id':'turn-b'}
    replacement=draft.propose(did,'修改第一列范围',[{'kind':'execute','column_ids':[a['id']],'product_ids':[pids[1]]}],use_active_turn=True)
    assert replacement['group_revision']==3
    targets={item['column_id']:item['product_ids'] for item in replacement['changes'][0]['targets']}
    assert targets=={a['id']:[pids[1]],b['id']:[pids[1]]}
    with pytest.raises(ValueError,match='已替换'):draft.confirm(did,combined['id'])

def test_confirmation_key_is_idempotent(dataset):
    did=dataset['id'];column=add_column(did,'idempotent');pid=draft.sheet(did)['rows'][0]['id']
    proposal=draft.propose(did,'执行',[{'kind':'execute','column_ids':[column['id']],'product_ids':[pid]}])
    first=draft.confirm(did,proposal['id'],confirmation_key='same-request')
    second=draft.confirm(did,proposal['id'],confirmation_key='same-request')
    assert [x['id'] for x in first]==[x['id'] for x in second]
    with Session() as session:assert len(list(session.scalars(select(BatchJob).where(BatchJob.proposal_id==proposal['id']))))==1

def test_business_discussion_remains_visible_and_operations_collapse():
    raw='我先读取当前工作表。\n\n这批样本以冷色为主，印花款应按底色判断。\n\n调用参数错误，重新提交。\n\n边界色需要保留未知。'
    presentation=build(raw)
    assert '这批样本以冷色为主' in presentation['discussion'] and '边界色需要保留未知' in presentation['discussion']
    assert '我先读取当前工作表' not in presentation['discussion']
    assert len(presentation['technical_notes'])==2


def test_presentation_does_not_repeat_owned_heading():
    presentation=build('**当前判断**\n\n本批需要先确定三个色系边界。')
    assert presentation['heading']=='当前判断'
    assert presentation['discussion']=='本批需要先确定三个色系边界。'
