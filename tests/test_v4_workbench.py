import copy,io,json,threading
import openpyxl,pytest
from sqlalchemy import select
from backend import draft,batch,text_facts,worksheet_query,field_catalog,importer,task_control
from backend.db import *
from test_draft import add,job,run

def test_dynamic_headers_preserved():
    wb=openpyxl.Workbook();ws=wb.active
    ws.append(['图片','父ASIN','Product Title','规格说明','详细参数','sku','2605月销','累计销量','编号'])
    ws.append(['','SYNTHETIC-PARENT-001','Girls 2 Piece Tankini','尺寸说明','Fabric: cotton','Size: 8-9 Years',3,18,'ABC-123'])
    b=io.BytesIO();wb.save(b);ds=importer.import_file(b.getvalue(),'variant.xlsx');cat=field_catalog.read(ds['id'])
    assert ds['mapping']['title']=='Product Title'
    assert [f['role'] for f in cat['fields']][3:5]==['parameters','parameters']
    assert cat['fields'][-1]['status']=='review'
    with Session() as s:p=s.scalar(select(Product).where(Product.dataset_id==ds['id']));source=text_facts.source_for(p)
    assert source['规格说明']=='尺寸说明' and source['详细参数']=='Fabric: cotton'
    assert draft.sheet(ds['id'])['dataset']['headers']==list(next(ws.values))

def test_scope_and_compound_confirmation(dataset):
    did=dataset['id'];col=add(did);rows=draft.sheet(did)['rows'];draft.correct(did,rows[0]['id'],col['id'],'2件套');draft.correct(did,rows[1]['id'],col['id'],'3件套')
    for keyword in ['2','2件','件套','2件套']:
        q=worksheet_query.query(did,[{'column_id':col['id'],'keywords':[keyword]}],save=True)
        assert rows[0]['id'] in q['product_ids']
    q=worksheet_query.query(did,[{'column_id':col['id'],'values':['2件套']}],save=True)
    p=draft.propose(did,'一次确认',[{'kind':'add','definition':{'key':'new','name':'新标签','type':'FACT','source':'text','definition':'根据原文判断套装件数','options':[]}},{'kind':'execute','column_ids':['$new'],'scope_id':q['scope_id']}])
    assert len(draft.sheet(did)['columns'])==1
    jobs=draft.confirm(did,p['id']);assert len(jobs)==1 and jobs[0]['spec']['product_ids']==[rows[0]['id']]
    assert len(draft.sheet(did)['columns'])==2

def test_old_task_success_and_failure_cannot_overwrite(dataset,monkeypatch):
    did=dataset['id'];c=add(did);pid=draft.sheet(did)['rows'][0]['id'];old=job(did,c,[pid]);new=job(did,c,[pid])
    with Session.begin() as s:draft.write_result(s,require(s,BatchJob,new['id']),pid,c,{'normalized_value':'正确','evidence':'新任务证据'})
    monkeypatch.setattr(batch,'request_json',lambda *a:(_ for _ in ()).throw(ValueError('旧任务错误')));run(old)
    cell=draft.sheet(did)['rows'][0]['cells'][c['id']];assert cell['value']=='正确' and cell['status']=='ready'
    with Session.begin() as s:draft.write_result(s,require(s,BatchJob,old['id']),pid,c,{'normalized_value':'旧值','evidence':'旧任务'})
    assert draft.sheet(did)['rows'][0]['cells'][c['id']]['value']=='正确'

def test_array_response_and_source_alias():
    assert batch.result_items([{'product_id':'a'}])==[{'product_id':'a'}]
    with pytest.raises(ValueError,match='对象'):batch.result_items({'results':[[]]})
    v={'facts':[{'attribute':'材质','value':'棉','state':'observed','evidence':[{'field':'参数','quote':'Cotton'}]}]}
    assert text_facts.validate(v,{'parameters':'Cotton'})['facts'][0]['evidence'][0]['field']=='parameters'

def test_failed_groundwork_does_not_block_raw_size(dataset,monkeypatch):
    did=dataset['id'];pre=text_facts.prepare(did);pid=pre['spec']['product_ids'][0]
    with Session.begin() as s:r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid));r.status='failed';r.error='颜色解析失败'
    c=add(did,judgment={'fields':['sku']})
    monkeypatch.setattr(batch,'request_json',lambda model,messages,*a:{'results':[{'product_id':p['product_id'],'normalized_value':'2件套','evidence':'原文SKU'} for p in json.loads(messages[-1]['content'])]})
    assert run(job(did,c,[pid]))['status']=='completed'

def test_text_rule_no_model(dataset,monkeypatch):
    did=dataset['id'];pid=draft.sheet(did)['rows'][0]['id']
    with Session.begin() as s:p=require(s,Product,pid);p.raw={**p.raw,'SKU':'Size: 8-9 Years'};p.fields={**p.fields,'sku':'Size: 8-9 Years'}
    c=add(did,method='text_rule',parser={'operation':'size_age','field':'sku','bins':[{'min':0,'max':4,'value':'小童'},{'min':4,'max':9,'value':'中童'},{'min':9,'max':13,'value':'大童'},{'min':13,'max':None,'value':'青少年'}],'unknown':'特殊'})
    monkeypatch.setattr(batch,'request_json',lambda *a:pytest.fail('deterministic text called model'))
    assert run(job(did,c,[pid]))['status']=='completed';assert draft.sheet(did)['rows'][0]['cells'][c['id']]['value']=='大童'

def test_progress_is_compact_and_archivable(dataset):
    did=dataset['id'];j=job(did,add(did))
    with Session() as s:e=list(s.scalars(select(DraftEvent).where(DraftEvent.kind=='job')))[-1]
    assert 'expected' not in e.payload['spec'] and len(json.dumps(e.payload))<2000
    with Session.begin() as s:require(s,BatchJob,j['id']).status='partial'
    task_control.disposition(did,j['id'],'archive');assert draft.state(did)['jobs'][0]['archived']

def test_empty_selection_and_facet_counts(dataset):
    did=dataset['id'];c=add(did);rows=draft.sheet(did)['rows'];draft.correct(did,rows[0]['id'],c['id'],'2件套')
    assert worksheet_query.query(did,[{'column_id':c['id'],'values':[]}])['count']==0
    q=worksheet_query.query(did,[{'column_id':c['id'],'values':['2件套']}],facet=c['id'])
    assert q['count']==1 and sum(v['count'] for v in q['values'])==10

def test_sparse_repair_keeps_exact_cells_and_manual(dataset):
    did=dataset['id'];a=add(did);b=add(did,key='other');rows=draft.sheet(did)['rows'];pids=[r['id'] for r in rows[:2]]
    p=draft.propose(did,'两列',[{'kind':'execute','targets':[{'column_id':a['id'],'product_ids':pids},{'column_id':b['id'],'product_ids':pids}]}]);j=draft.confirm(did,p['id'])[0]
    with Session.begin() as s:
        saved=require(s,BatchJob,j['id']);saved.status='partial';saved.errors={t:'格式错误' for t in saved.spec['expected']}
    tokens=[pids[0]+':'+a['id'],pids[1]+':'+b['id']]
    p=task_control.repair(did,j['id'],tokens);jobs=draft.confirm(did,p['id'])
    assert {t for j in jobs for t in j['spec']['expected']}==set(tokens)
    draft.correct(did,pids[1],a['id'],'人工保留')
    with pytest.raises(ValueError,match='保护'):task_control.repair(did,j['id'],[pids[1]+':'+a['id']])

@pytest.mark.parametrize('selected_indexes', [[0], [1], [0,1]])
def test_sparse_confirmation_intersects_each_column_separately(dataset,selected_indexes):
    did=dataset['id'];a=add(did);b=add(did,key='second')
    pids=[r['id'] for r in draft.sheet(did)['rows'][:3]]
    p=draft.propose(did,'不同商品修复不同列',[
        {'kind':'execute','column_ids':[a['id']],'product_ids':[pids[0]]},
        {'kind':'execute','column_ids':[b['id']],'product_ids':[pids[1]]}])
    jobs=draft.confirm(did,p['id'],scope=[pids[i] for i in selected_indexes])
    expected={pids[i]+':'+[a,b][i]['id'] for i in selected_indexes}
    assert {token for job in jobs for token in job['spec']['expected']}==expected
    assert all(job['spec']['product_ids'] for job in jobs)

def test_sparse_confirmation_rejects_scope_outside_proposal(dataset):
    did=dataset['id'];a=add(did);pids=[r['id'] for r in draft.sheet(did)['rows'][:2]]
    p=draft.propose(did,'修复',[{'kind':'execute','column_ids':[a['id']],'product_ids':pids[:1]}])
    with pytest.raises(ValueError,match='范围'):
        draft.confirm(did,p['id'],scope=pids)
    assert next(x for x in draft.state(did)['proposals'] if x['id']==p['id'])['status']=='pending'

def test_compound_sample_caps_distinct_products_across_jobs(dataset):
    did=dataset['id'];a=add(did);b=add(did,key='second')
    with Session.begin() as s:
        for row in range(100,125):
            s.add(Product(dataset_id=did,row=row,anchor=str(row),raw={},fields={}))
    pids=[r['id'] for r in draft.sheet(did)['rows']]
    p=draft.propose(did,'统一抽样',[
        {'kind':'execute','column_ids':[a['id']],'product_ids':pids[:15]},
        {'kind':'execute','column_ids':[b['id']],'product_ids':pids[15:]}])
    jobs=draft.confirm(did,p['id'],sample=True)
    assert len(jobs)==2
    assert {pid for job in jobs for pid in job['spec']['product_ids']}==set(pids[:20])

def test_mapping_removal_and_extra_details_stale(dataset):
    did=dataset['id'];cat=field_catalog.read(did);sku=next(f for f in cat['fields'] if f['role']=='sku')
    c=add(did,fields=['sku']);j=job(did,c)
    with Session.begin() as s:
        for pid in j['spec']['product_ids']:draft.write_result(s,require(s,BatchJob,j['id']),pid,c,{'normalized_value':'原值','evidence':'SKU'})
    raw=copy.deepcopy(draft.sheet(did)['rows'][0]['raw'])
    field_catalog.confirm(did,[{'id':sku['id'],'role':'other'}],cat['revision'])
    data=draft.sheet(did)
    assert 'sku' not in data['dataset']['mapping'] and 'sku' not in data['rows'][0]['fields']
    assert data['rows'][0]['raw']==raw and data['rows'][0]['cells'][c['id']]['status']=='stale'

def test_visual_columns_share_pixel_observation_and_both_keeps_text(dataset,monkeypatch):
    did=dataset['id'];a=add(did,source='both',fields=['sku']);b=add(did,key='pattern',source='image');pid=draft.sheet(did)['rows'][0]['id'];calls=[]
    from backend.db import Asset
    original=draft.sheet(did)['rows'][0]['assets'][0]
    with Session.begin() as s:s.add(Asset(product_id=pid,source='upload',path=original['path'],sha256=original['sha256'],image_type='supplemental'))
    def observe(*args):calls.append(args);return {'observation':'上衣前领口圆弧，下装纯色','asset_id':args[0]['id']}
    monkeypatch.setattr(batch,'observation',observe)
    requests=[]
    def classify(m,msg,*a):
        inputs=json.loads(msg[-1]['content']);requests.extend(inputs)
        return {'results':[{'product_id':p['product_id'],'normalized_value':'2件套','evidence':'清晰可见'} for p in inputs]}
    monkeypatch.setattr(batch,'request_json',classify)
    p=draft.propose(did,'共同观察',[{'kind':'execute','targets':[{'column_id':a['id'],'product_ids':[pid]},{'column_id':b['id'],'product_ids':[pid]}]}]);j=run(draft.confirm(did,p['id'])[0])
    assert j['status']=='completed' and len(calls)==1
    assert calls[0][6] is None and calls[0][0]['source']=='excel' and j['metrics']['shared_visual_hits']==1
    assert any(p.get('text',{}).get('sku') for p in requests) and any('text' not in p for p in requests)
    assert j['metrics']['main_image_observations']==1 and j['metrics']['supplemental_images_ignored']==1

def test_single_cardinality_preserves_conflicting_candidate_without_expansion(dataset):
    did=dataset['id'];c=add(did,cardinality='single',options=['圆领','方领']);pid=draft.sheet(did)['rows'][0]['id'];j=job(did,c,[pid])
    with Session.begin() as s:draft.write_result(s,require(s,BatchJob,j['id']),pid,c,{'normalized_value':['圆领','方领'],'attributes':[{'attribute':'圆领','value':True,'evidence':'候选1'},{'attribute':'方领','value':True,'evidence':'候选2'}]})
    data=draft.state(did);assert data['rows'][0]['cells'][c['id']]['status']=='review'
    assert not any(p['status']=='pending' and p['changes'][0]['kind']=='expand' for p in data['proposals'])
