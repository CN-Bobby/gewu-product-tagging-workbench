import json
from pathlib import Path
import pytest
from sqlalchemy import select
from backend import draft,batch,templates,importer,result_reuse
from backend.db import *
from test_draft import add,run
from fixture_data import FIXTURE_PATH


def new_month(dataset):
    return importer.import_file(FIXTURE_PATH.read_bytes(),'二月.xlsx',workspace_id=dataset['workspace_id'])


def test_template_revisions_and_months_are_independent(dataset):
    did=dataset['id'];column=add(did);month=new_month(dataset)
    first=templates.save(did,'件数标准',[column['id']])
    original=json.dumps(first['data'],sort_keys=True)
    proposal=templates.propose_copy(month['id'],first['id'])
    assert not draft.sheet(month['id'])['columns']
    draft.confirm(month['id'],proposal['id'])
    copied=draft.sheet(month['id'])['columns'][0]
    assert copied['id']!=column['id'] and copied['origin']['template']['revision_id']==first['id']
    second=templates.save(did,'件数标准',[column['id']],first['template_id'])
    assert second['revision']==2
    with Session() as s:assert json.dumps(require(s,TemplateRevision,first['id']).data,sort_keys=True)==original
    unrelated=importer.import_file(FIXTURE_PATH.read_bytes(),'其他项目.xlsx')
    with pytest.raises(ValueError,match='同项目'):templates.propose_copy(unrelated['id'],first['id'])


def test_exact_reuse_manual_trace_and_force_recompute(dataset,monkeypatch):
    from backend import vision
    monkeypatch.setattr(vision,'provider_config',lambda model:('https://test.invalid/v1/chat/completions','test-only',model))
    requests=[]
    def response(model,messages,*args):
        inputs=json.loads(messages[-1]['content']);requests.extend(inputs)
        return {'results':[{'product_id':p['product_id'],'normalized_value':'2件套','raw_value':'2pcs','evidence':'标题明确声明'} for p in inputs]}
    monkeypatch.setattr(batch,'request_json',response)
    did=dataset['id'];c=add(did)
    proposal=draft.propose(did,'首次',[{'kind':'execute','column_ids':[c['id']]}])
    run(draft.confirm(did,proposal['id'])[0]);assert len(requests)==10
    row=draft.sheet(did)['rows'][0];draft.correct(did,row['id'],c['id'],'人工值')
    standard=templates.save(did,'标准',[c['id']]);month=new_month(dataset)
    copied=templates.propose_copy(month['id'],standard['id'],execute=True)
    jobs=draft.confirm(month['id'],copied['id']);result=run(jobs[0])
    assert result['status']=='completed' and result['metrics']['result_reuse_hits']==10
    assert len(requests)==10
    sheet=draft.sheet(month['id']);newc=sheet['columns'][0];cell=sheet['rows'][0]['cells'][newc['id']]
    assert cell['value']=='人工值' and cell['evidence']['reuse_source']['manual']
    frozen=draft.save_version(month['id']);before=draft.sheet(month['id'],frozen['id'])
    force=draft.propose(month['id'],'强制',[{'kind':'execute','column_ids':[newc['id']],'product_ids':[sheet['rows'][0]['id']],'overwrite':True,'reuse_results':True,'force_recompute':True}])
    run(draft.confirm(month['id'],force['id'])[0]);assert len(requests)==11
    assert draft.sheet(month['id'],frozen['id'])==before
    assert draft.sheet(did)['rows'][0]['cells'][c['id']]['value']=='人工值'


def test_changed_required_text_does_not_reuse(dataset,monkeypatch):
    from backend import vision
    monkeypatch.setattr(vision,'provider_config',lambda model:('https://test.invalid','test-only',model))
    count=[]
    def response(model,messages,*args):
        rows=json.loads(messages[-1]['content']);count.extend(rows)
        return {'results':[{'product_id':r['product_id'],'normalized_value':'2件套','evidence':'文字'} for r in rows]}
    monkeypatch.setattr(batch,'request_json',response)
    c=add(dataset['id']);p=draft.propose(dataset['id'],'首次',[{'kind':'execute','column_ids':[c['id']]}]);run(draft.confirm(dataset['id'],p['id'])[0])
    standard=templates.save(dataset['id'],'标准',[c['id']]);month=new_month(dataset)
    with Session.begin() as s:
        p=s.scalar(select(Product).where(Product.dataset_id==month['id']).order_by(Product.row));p.fields={**p.fields,'title':'Changed product title'};p.raw={**p.raw,'商品标题':'Changed product title'}
    p=templates.propose_copy(month['id'],standard['id'],execute=True);j=run(draft.confirm(month['id'],p['id'])[0])
    assert len(count)==11 and j['metrics']['result_reuse_hits']==9


def test_template_preserves_expanded_structure_and_dependencies(dataset):
    did=dataset['id'];parent=add(did,key='pattern',name='图案',options=['花卉','条纹'])
    proposal=draft.propose(did,'拆列',[{'kind':'expand','column_id':parent['id'],'attributes':['花卉','条纹']}]);draft.confirm(did,proposal['id'])
    children=[c for c in draft.sheet(did)['columns'] if c['definition'].get('parent_id')]
    standard=templates.save(did,'图案标准',[c['id'] for c in children]);month=new_month(dataset)
    proposal=templates.propose_copy(month['id'],standard['id']);draft.confirm(month['id'],proposal['id'])
    columns=draft.sheet(month['id'])['columns'];new_parent=next(c for c in columns if c['definition'].get('expanded'))
    assert len(columns)==3 and new_parent['id']!=parent['id']
    assert {c['definition'].get('attribute') for c in columns if c['definition'].get('parent_id')}=={'花卉','条纹'}
    assert all(c['definition']['parent_id']==new_parent['id'] for c in columns if c['definition'].get('parent_id'))
