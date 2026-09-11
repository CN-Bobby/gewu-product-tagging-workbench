import copy,json,threading
import pytest
from sqlalchemy import select
from backend import draft,batch,attributes,text_facts
from backend.db import *
from test_draft import add,job,run

def expand(did,parent,names):
    p=draft.propose(did,'拆列',[{'kind':'expand','column_id':parent['id'],'attributes':names}]);draft.confirm(did,p['id'])
    return {c['definition']['attribute']:c for c in draft.sheet(did)['columns'] if c['definition'].get('parent_id')==parent['id']}

def test_new_attribute_reuses_discovering_cell_evidence(dataset):
    did=dataset['id'];parent=add(did,'pattern');child=expand(did,parent,['花卉'])['花卉'];pid=draft.sheet(did)['rows'][0]['id']
    j=job(did,child,[pid])
    with Session.begin() as s:
        task=require(s,BatchJob,j['id']);draft.write_result(s,task,pid,child,{'normalized_value':False,'evidence':'无花卉，有条纹','attributes':[{'attribute':'花卉','value':False,'evidence':'无花卉'},{'attribute':'条纹','value':True,'evidence':'有条纹'}]});task.status='completed'
    pending=next(p for p in draft.state(did)['proposals'] if p['status']=='pending' and p['changes'][0]['kind']=='expand')
    draft.confirm(did,pending['id']);data=draft.sheet(did);stripe=next(c for c in data['columns'] if c['definition'].get('attribute')=='条纹')
    cell=data['rows'][0]['cells'][stripe['id']]
    assert cell['value'] is True and child['id'] in json.dumps(cell['evidence'])

def test_mapping_attributes_recompute_without_model(dataset,monkeypatch):
    did=dataset['id'];base=add(did,'color');pid=draft.sheet(did)['rows'][0]['id'];draft.correct(did,pid,base['id'],'红')
    parent=add(did,'temperature',method='mapping',depends_on=[base['id']],mapping={'红':'暖','蓝':'冷'})
    cols=expand(did,parent,['暖','冷'])
    draft.correct(did,pid,base['id'],'蓝')
    rows=draft.sheet(did)['rows'];assert rows[0]['cells'][cols['冷']['id']]['value'] is True
    assert rows[0]['cells'][cols['暖']['id']]['value'] is None
    monkeypatch.setattr(batch,'request_json',lambda *a:pytest.fail('deterministic projection called model'))
    assert run(job(did,cols['冷'],[pid],overwrite=True))['status']=='completed'

def test_mapping_worker_keeps_changed_dependency_conflict(dataset):
    did=dataset['id'];base=add(did,'color');pid=draft.sheet(did)['rows'][0]['id'];draft.correct(did,pid,base['id'],'红')
    col=add(did,'group',method='mapping',depends_on=[base['id']],mapping={'红':'暖','蓝':'冷'});j=job(did,col,[pid],overwrite=True)
    draft.correct(did,pid,base['id'],'蓝');run(j)
    cell=draft.sheet(did)['rows'][0]['cells'][col['id']]
    assert cell['conflict'] and cell['value']=='冷'

def test_vision_requirement_includes_judgment(dataset,monkeypatch):
    monkeypatch.delenv('TAG_DISABLE_VISUAL_TAGGING',raising=False)
    did=dataset['id'];col=add(did,'color',source='image',judgment={'include':'只观察上衣'});seen=[]
    def observe(asset,model,requirements,*rest):seen.append(requirements);return {'observation':'蓝色'}
    def fake(model,messages,*args):return {'results':[{'product_id':p['product_id'],'normalized_value':'蓝色','evidence':'图片上衣'} for p in json.loads(messages[-1]['content'])]}
    monkeypatch.setattr(batch,'observation',observe);monkeypatch.setattr(batch,'request_json',fake)
    pid=draft.sheet(did)['rows'][0]['id'];run(job(did,col,[pid]))
    p=draft.propose(did,'规则修订',[{'kind':'revise','column_id':col['id'],'definition':{**col['definition'],'judgment':{'include':'观察所有配件'}}}]);draft.confirm(did,p['id'])
    col=next(c for c in draft.sheet(did)['columns'] if c['id']==col['id']);run(job(did,col,[pid],overwrite=True))
    assert seen[0]!=seen[1] and '所有配件' in seen[1]

def test_attribute_result_rejects_contradictory_states():
    with pytest.raises(ValueError):attributes.validate_result({'normalized_value':False,'evidence':'无花卉','attributes':[{'attribute':'花卉','value':True,'evidence':'有花卉'}]},{'attribute':'花卉'})

def test_judgment_fields_are_respected(dataset,monkeypatch):
    did=dataset['id'];col=add(did,judgment={'fields':['title']});seen=[]
    def fake(model,messages,*args):
        inputs=json.loads(messages[-1]['content']);seen.extend(inputs)
        return {'results':[{'product_id':p['product_id'],'normalized_value':'2件套','evidence':'标题'} for p in inputs]}
    monkeypatch.setattr(batch,'request_json',fake);run(job(did,col))
    assert all(set(p['text'])=={'title'} for p in seen)

@pytest.mark.parametrize('projected',[False,True])
def test_execute_columns_follows_dependencies(dataset,monkeypatch,projected):
    did=dataset['id'];base=add(did,'color');group=add(did,'group',method='mapping',depends_on=[base['id']],mapping={'蓝':'冷','红':'暖'})
    target=expand(did,group,['冷','暖'])['冷'] if projected else group
    pid=draft.sheet(did)['rows'][0]['id'];calls=[]
    def fake(model,messages,*args):
        calls.append(1)
        return {'results':[{'product_id':p['product_id'],'normalized_value':'蓝','evidence':'标题蓝色'} for p in json.loads(messages[-1]['content'])]}
    monkeypatch.setattr(batch,'request_json',fake)
    p=draft.propose(did,'同时执行',[{'kind':'execute','targets':[{'column_id':target['id'],'product_ids':[pid]},{'column_id':base['id'],'product_ids':[pid]}],'overwrite':True}]);j=draft.confirm(did,p['id'])[0]
    result=run(j);assert result['status']=='completed' and len(calls)==1
    cell=draft.sheet(did)['rows'][0]['cells'][target['id']]
    assert cell['value']==(True if projected else '冷') and not cell['conflict'] and cell['status']=='ready'

def test_replaced_text_job_cannot_resume_or_overwrite(dataset,monkeypatch):
    did=dataset['id'];old=text_facts.prepare(did)
    with Session.begin() as s:require(s,BatchJob,old['id']).status='paused'
    new=text_facts.prepare(did)
    with pytest.raises(ValueError,match='替代'):batch.resume(old['id'])
    monkeypatch.setattr(batch,'request_json',lambda *a:pytest.fail('obsolete text task must not request model'))
    assert run(old)['status']=='partial'
    assert all(r['provenance']['pending_job_id']==new['id'] and r['status']=='pending' for r in text_facts.records(did))

def test_same_name_dependency_proposals_keep_column_identity(dataset):
    did=dataset['id'];base=add(did,'color');a=add(did,'first',name='分类',depends_on=[base['id']]);b=add(did,'second',name='分类',depends_on=[base['id']])
    pid=draft.sheet(did)['rows'][0]['id'];draft.correct(did,pid,base['id'],'蓝')
    proposals=[p for p in draft.state(did)['proposals'] if p['status']=='pending' and p['changes'][0].get('automatic_dependency_update')]
    assert len(proposals)==1
    assert {change['column_ids'][0] for change in proposals[0]['changes']}=={a['id'],b['id']}
