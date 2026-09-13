"""P0 F01: a truncated finish_reason must not discard an already-usable response."""
import json
import threading
import pytest
from backend import batch,draft,source_label_inference
from backend.db import *
from test_source_label_inference import seed_partial_labels

TARGET='依据商品图判定上衣整体造型结构，以领型、肩带形态与胸杯造型综合区分的复合描述值'


def truncated_standard():
    """Shape matches the real beta.6 response: valid JSON, trailing keys never emitted."""
    return {'target':TARGET,'cardinality':'single','source':'image',
        'visual_suitability':{'status':'suitable','reason':'正面商品图可直接观察领口与肩带'},
        'observed_value_rules':[{'id':'hv_%d'%i,'positive_features':['特征%d'%i],'exclude':[],'representative_product_ids':[]} for i in range(8)],
        'model_discovered_candidates':[],'unknown_policy':'证据不足保留为空','not_applicable_policy':'不适用时记录原因',
        'image_unjudgeable_policy':'图片无法判断时不得猜测',
        'conflicts':[{'product_id':'p%d'%i,'type':'within_label','reason':'边界模糊'} for i in range(4)],
        'unresolved':['未标样本尚未做视觉核查']}


def payload(content,finish_reason='length',usage=None):
    return {'choices':[{'finish_reason':finish_reason,'message':{'content':content}}],
        'usage':usage or {'prompt_tokens':100,'completion_tokens':8192}}


class Response:
    def __init__(self,body):self.status_code=200;self._body=body
    def json(self):return self._body


class Client:
    bodies=[];sent=[]
    def __init__(self,timeout):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def post(self,endpoint,headers,json):
        Client.sent.append(json);return Response(Client.bodies.pop(0))


def offline(monkeypatch,bodies):
    Client.bodies=list(bodies);Client.sent=[]
    monkeypatch.setattr(batch.httpx,'Client',Client)
    monkeypatch.setattr(batch,'provider_config',lambda *a,**k:('https://test.invalid/v1','key','glm-5.3-flash'))


def test_truncated_but_valid_json_is_accepted(monkeypatch):
    offline(monkeypatch,[payload(json.dumps(truncated_standard(),ensure_ascii=False))])
    metrics={}
    result=batch.request_json('CS/z-ai/glm-5.3-flash',[{'role':'user','content':'x'}],metrics,threading.Lock())
    assert result['target']==TARGET and len(result['observed_value_rules'])==8
    assert metrics['truncated_responses']==1
    assert Client.sent[0]['max_tokens']==8192


def test_empty_content_still_fails(monkeypatch):
    offline(monkeypatch,[payload('')])
    with pytest.raises(ValueError) as error:
        batch.request_json('CS/z-ai/glm-5.3-flash',[{'role':'user','content':'x'}],{},threading.Lock())
    assert '截断' in str(error.value)


def test_cut_off_json_still_fails(monkeypatch):
    offline(monkeypatch,[payload('{"target":"半句话没写完')])
    with pytest.raises(ValueError) as error:
        batch.request_json('CS/z-ai/glm-5.3-flash',[{'role':'user','content':'x'}],{},threading.Lock())
    assert '截断' in str(error.value)


def test_attempt_keeps_evidence_for_reasoning_tokens(monkeypatch):
    offline(monkeypatch,[payload(json.dumps(truncated_standard(),ensure_ascii=False))])
    metrics={'job_id':'job-evidence'}
    batch.request_json('CS/z-ai/glm-5.3-flash',[{'role':'user','content':'x'}],metrics,threading.Lock())
    with Session() as s:
        attempt=s.scalar(select(ModelAttempt).where(ModelAttempt.job_id=='job-evidence'))
    data=attempt.data
    assert data['finish_reason']=='length'
    assert data['usage']=={'prompt_tokens':100,'completion_tokens':8192}
    assert data['content_chars']>0 and data['reasoning_chars']==0


def valid_body():
    return payload(json.dumps(truncated_standard(),ensure_ascii=False))


def prepare(dataset,monkeypatch,seeded=True):
    did=dataset['id']
    if seeded:seed_partial_labels(did,total=10)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['ASIN']})
    monkeypatch.setattr('backend.model_settings.defaults',lambda:('test/model','test/vision'))
    monkeypatch.setattr('backend.model_settings.require_validated',lambda *a,**k:True)
    return did,item


def test_synthesize_accepts_truncated_standard_with_single_request(dataset,monkeypatch):
    did,item=prepare(dataset,monkeypatch)
    calls=[]
    def fake(model,messages,metrics,lock,credential_id=None):
        metrics['truncated_responses']=metrics.get('truncated_responses',0)+1
        calls.append(messages);return truncated_standard()
    monkeypatch.setattr(batch,'request_json',fake)
    metrics={'requests':0}
    standard=source_label_inference._synthesize(item['profile'],[],[],None,'test/model',None,metrics,threading.Lock())
    assert standard['target']==TARGET
    assert standard['questions']==[] and standard['normalization_suggestions']==[]
    assert standard['generation']['truncated'] is True
    assert len(calls)==1


def test_execute_completes_instead_of_failing_the_draft(dataset,monkeypatch):
    did,item=prepare(dataset,monkeypatch)
    job=source_label_inference.analyze(did,item['id'],{'text_model':'test/model'})
    def fake(model,messages,metrics,lock,credential_id=None):
        metrics['truncated_responses']=metrics.get('truncated_responses',0)+1
        return truncated_standard()
    monkeypatch.setattr(batch,'request_json',fake)
    batch.active[job['id']]=threading.Event();batch.execute(job['id'])
    with Session() as s:
        stored=require(s,BatchJob,job['id']);record=require(s,SourceLabelDraft,item['id'])
    assert stored.status=='completed'
    assert record.status!='failed' and record.data['standard']['target']==TARGET
    assert record.data['standard']['generation']['truncated'] is True
    assert stored.metrics['truncated_responses']==1


def failed_draft_with_attempt(did,item):
    with draft.WRITE_LOCK,Session.begin() as s:
        job=BatchJob(dataset_id=did,status='partial',metrics={'requests':1,'vision_requests':0},spec={
            'kind':'source_label_inference','source_label_draft_id':item['id'],
            'text_model':'test/model','vision_model':'test/vision','requires_visual':False,
            'product_ids':item['sample_plan']['initial_product_ids'],'columns':[]})
        s.add(job);s.flush()
        record=require(s,SourceLabelDraft,item['id']);record.job_id=job.id;record.status='failed'
        from backend.task_control import reject_candidate
        reject_candidate(s,job.id,'test/model',{'code':'model_request','attempt':1,'http_status':200,
            'finish_reason':'length','seconds':1.0,'error':'输出截断',
            'content':json.dumps(truncated_standard(),ensure_ascii=False),'input_sha256':'deadbeef'})
        return job.id


def test_recover_from_attempt_replays_stored_content_without_model_call(dataset,monkeypatch):
    did,item=prepare(dataset,monkeypatch)
    failed_draft_with_attempt(did,item)
    monkeypatch.setattr(batch,'request_json',lambda *a,**k:pytest.fail('恢复不得调用模型'))
    recovered=source_label_inference.recover_from_attempt(did,item['id'])
    assert recovered['status']=='review_required'
    assert recovered['standard']['target']==TARGET
    assert recovered['standard']['generation']['recovered_from_attempt']
    assert len(recovered['standard']['observed_human_values'])==4


def test_program_baseline_is_marked_unverified(dataset,monkeypatch):
    did,item=prepare(dataset,monkeypatch)
    baseline=source_label_inference.program_baseline(did,item['id'])
    standard=baseline['standard']
    assert standard['generation']=={'source':'program','model_verified':False,'visual_executed':False}
    assert standard['visual_suitability']['status']=='unreviewed'
    assert len(standard['target'])>=5
    assert len(standard['observed_human_values'])==4


def test_view_exposes_recovery_and_attempt_counts(dataset,monkeypatch):
    did,item=prepare(dataset,monkeypatch)
    failed_draft_with_attempt(did,item)
    view=source_label_inference.read(did,item['id'])
    assert view['recovery']['available'] is True
    assert view['recovery']['finish_reason']=='length'
    assert view['attempts']=={'text_requests':1,'vision_requests':0}
