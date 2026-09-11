import threading,copy
from sqlalchemy import select
from backend.db import *
from backend import draft,batch,conversation,service
from test_draft import add,job,run

def test_legacy_migration_keeps_evidence_and_distinct_columns(dataset):
    did=dataset['id'];d={'key':'color','name':'颜色','type':'FACT','source':'text','definition':'根据文字提取颜色名称','options':['blue']}
    with Session.begin() as s:
        sc=Schema(dataset_id=did,version=1,definitions=[d],status='locked');s.add(sc);s.flush()
        pid=s.scalar(select(Product.id).where(Product.dataset_id==did));r=Run(dataset_id=did,schema_id=sc.id,kind='sample',product_ids=[pid]);s.add(r);s.flush();s.add(Tag(run_id=r.id,product_id=pid,values=[{'key':'color','raw_value':'blue','normalized_value':'blue','evidence':'title'}]))
        s.add(Schema(dataset_id=did,version=2,definitions=[{**d,'definition':'根据文字提取另一不同语义'}],status='locked'))
    sheet=draft.sheet(did);assert len(sheet['columns'])==2
    cell=next(r['cells'][sheet['columns'][0]['id']] for r in sheet['rows'] if r['id']==pid)
    assert cell['value']=='蓝色系' and cell['raw_value']=='blue' and cell['evidence']['legacy_run_id']
    assert len(draft.sheet(did)['columns'])==2

def test_pause_before_request_and_cancel(dataset,monkeypatch):
    did=dataset['id'];c=add(did);j=job(did,c)
    monkeypatch.setattr(batch,'request_json',lambda *a:(_ for _ in ()).throw(AssertionError('must not dispatch')))
    event=threading.Event();event.set();batch.active[j['id']]=event;batch.execute(j['id'])
    with Session() as s:assert require(s,BatchJob,j['id']).status=='paused'
    j=job(did,c);event=threading.Event();batch.active[j['id']]=event;batch.stop(j['id'],True);batch.execute(j['id'])
    with Session() as s:assert require(s,BatchJob,j['id']).status=='cancelled'

def test_model_dependency_marks_stale_and_retains_late_candidate(dataset):
    did=dataset['id'];up=add(did);pid=draft.sheet(did)['rows'][0]['id'];draft.correct(did,pid,up['id'],'2件套')
    derived=add(did,'derived',method='model',depends_on=[up['id']]);j=job(did,derived,[pid])
    draft.correct(did,pid,up['id'],'3件套')
    with draft.WRITE_LOCK,Session.begin() as s:draft.write_result(s,require(s,BatchJob,j['id']),pid,derived,{'normalized_value':'旧的模型结果'})
    cell=draft.sheet(did)['rows'][0]['cells'][derived['id']]
    assert cell['status']=='stale' and cell['conflict']
    assert any(p['title']=='更新受影响的派生标签' for p in draft.state(did)['proposals'])

def test_bound_user_confirmation_only(dataset,monkeypatch):
    did=dataset['id'];p=draft.propose(did,'测试人工指令',[{'kind':'add','definition':{'key':'test','name':'测试列','type':'FACT','source':'text','definition':'测试判断定义内容','options':[]}}])
    import pytest
    with pytest.raises(ValueError):conversation.start(did,'确认执行')
    result=conversation.start(did,'确认执行',proposal_id=p['id']);assert result['confirmed']
    assert len(draft.sheet(did)['columns'])==1

def test_visual_cache_fingerprint_and_requirements(dataset,monkeypatch):
    from backend.harness import VISION_MODEL
    calls=[]
    def fake(*args):calls.append(1);return {'observation':'蓝色泳衣','unknown':[]}
    monkeypatch.setattr(batch,'request_json',fake)
    a=next(a for a in draft.sheet(dataset['id'])['rows'][0]['assets'] if a['path']);metrics={};lock=threading.Lock()
    assert not batch.observation(a,VISION_MODEL,'颜色',False,metrics,lock)['cached']
    assert batch.observation({**a,'id':'same-image-other-id'},VISION_MODEL,'颜色',False,metrics,lock)['cached']
    assert not batch.observation(a,VISION_MODEL,'领口结构',False,metrics,lock)['cached']
    assert len(calls)==2 and metrics['cache_hits']==1

def test_legacy_transform_chain_migration(dataset):
    from test_workflow import schema,finish
    did=dataset['id'];sc=schema(dataset);r=service.create_run(did,sc['id'],'sample','test');finish(r)
    t=service.propose_transform(r['id'],'color','family','蓝色归为蓝色系',{'蓝色':'蓝色系'});service.approve_transform(t['id'])
    t=service.propose_transform(r['id'],'family','temperature','蓝色系归为冷色',{'蓝色系':'冷色'});service.approve_transform(t['id'])
    data=draft.sheet(did);assert len(data['columns'])==3
    last=next(c for c in data['columns'] if c['key']=='temperature')
    assert all(row['cells'][last['id']]['value']=='冷色' for row in data['rows'])
