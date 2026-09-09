import io
import openpyxl
import pytest
from sqlalchemy import select

from backend import attributes,draft,importer,task_control,text_rules
from backend.db import Session,Product,Cell,BatchJob,require
from test_draft import add


def workbook_bytes(headers,row):
    book=openpyxl.Workbook();sheet=book.active;sheet.append(headers);sheet.append(row)
    data=io.BytesIO();book.save(data);return data.getvalue()


def test_zero_sku_falls_back_to_parent_asin_with_explicit_mapping():
    data=workbook_bytes(['SKU','父ASIN','标题'],[0,'B0F83KJBW5','泳衣'])
    preview=importer.inspect(data)
    assert preview['sample'][0]['anchor']=='B0F83KJBW5'
    assert preview['sample'][0]['identity_source']=='parent_asin'
    dataset=importer.import_file(data,'泳衣样本.xlsx',mapping={'sku':'SKU','parent_asin':'父ASIN','title':'标题'})
    with Session() as session:product=session.scalar(select(Product).where(Product.dataset_id==dataset['id']))
    assert product.anchor=='B0F83KJBW5' and product.identity_source=='parent_asin'
    assert product.raw['SKU']==0 and dataset['source_filename']=='泳衣样本.xlsx'


def test_result_shapes_keep_scalar_simple_and_multi_strict():
    value={'normalized_value':'16','evidence':'SKU Size:16','attributes':[{'attribute':'青少年','value':True,'evidence':'模型多余字段'}]}
    result=attributes.validate_result(value,{'result_shape':'scalar_open','options':[]})
    assert result['normalized_value']=='16' and 'attributes' not in result and result['format_warnings']
    with pytest.raises(ValueError,match='候选值'):
        attributes.validate_result({'normalized_value':'16'}, {'result_shape':'scalar_enum','options':['中童','青少年']})
    attributes.validate_result({'normalized_value':['花卉','条纹'],'attributes':[{'attribute':'花卉','value':True,'evidence':'花朵'},{'attribute':'条纹','value':True,'evidence':'条纹'}]}, {'result_shape':'multi_enum','options':['花卉','条纹']})
    attributes.validate_result({'normalized_value':None,'evidence':'无法判断','attributes':[{'attribute':'花卉','value':None,'evidence':'图案不清楚'},{'attribute':'条纹','value':None,'evidence':'图案不清楚'}]}, {'result_shape':'multi_enum','options':['花卉','条纹']})
    with pytest.raises(ValueError,match='正命中'):
        attributes.validate_result({'normalized_value':None,'evidence':'矛盾','attributes':[{'attribute':'花卉','value':True,'evidence':'明确花朵'}]}, {'result_shape':'multi_enum','options':['花卉']})


@pytest.mark.parametrize(('raw','value'),[('Color: Blue | Size: 16','16'),('Size: 4 | Color: Blue','4'),('Size: 3-4T','4'),('Size: 5T','5'),('Size: Medium',None)])
def test_size_value_is_deterministic(raw,value):
    result=text_rules.apply({'operation':'size_value','field':'sku','unknown':None},{'sku':raw})
    assert result['normalized_value']==value and result['method']=='text_rule'


def test_targets_keep_exact_cells_and_reject_legacy_multicolumn(dataset):
    did=dataset['id'];a=add(did);b=add(did,key='other');pids=[r['id'] for r in draft.sheet(did)['rows'][:2]]
    with pytest.raises(ValueError,match='targets'):
        draft.propose(did,'旧式多列',[{'kind':'execute','column_ids':[a['id'],b['id']],'product_ids':pids}])
    proposal=draft.propose(did,'精确多列',[{'kind':'execute','targets':[{'column_id':a['id'],'product_ids':[pids[0]]},{'column_id':b['id'],'product_ids':[pids[1]]}]}])
    job=draft.confirm(did,proposal['id'])[0]
    assert set(job['spec']['expected'])=={pids[0]+':'+a['id'],pids[1]+':'+b['id']}


def test_compatible_revision_preserves_success_and_retries_only_failure(dataset):
    did=dataset['id'];col=add(did);rows=draft.sheet(did)['rows'];success,failed=rows[0]['id'],rows[1]['id']
    with Session.begin() as session:
        ready=draft.get_cell(session,success,col['id']);ready.value='2件套';ready.status='ready'
        bad=draft.get_cell(session,failed,col['id']);bad.status='failed'
    definition={**col['definition'],'definition':col['definition']['definition']+'；补充纯数字写法'}
    proposal=draft.propose(did,'兼容修订',[{'kind':'revise','column_id':col['id'],'definition':definition},{'kind':'execute','targets':[{'column_id':col['id'],'product_ids':[failed]}]}])
    assert proposal['changes'][0]['impact_mode']=='preserve_compatible'
    job=draft.confirm(did,proposal['id'])[0];sheet=draft.sheet(did)
    assert set(job['spec']['expected'])=={failed+':'+col['id']}
    kept=next(r for r in sheet['rows'] if r['id']==success)['cells'][col['id']]
    assert kept['status']=='ready' and kept['value']=='2件套' and kept['evidence']['accepted_through_revision']==2


def test_semantic_revision_invalidates_nonmanual_results(dataset):
    did=dataset['id'];col=add(did,options=['中童','青少年']);pid=draft.sheet(did)['rows'][0]['id']
    with Session.begin() as session:
        cell=draft.get_cell(session,pid,col['id']);cell.value='中童';cell.status='ready'
    definition={**col['definition'],'options':['中童']}
    proposal=draft.propose(did,'删除候选',[{'kind':'revise','column_id':col['id'],'definition':definition}])
    assert proposal['changes'][0]['impact_mode']=='invalidate_all'
    draft.confirm(did,proposal['id'])
    assert draft.sheet(did)['rows'][0]['cells'][col['id']]['status']=='stale'


def test_cross_job_repair_scope_is_immutable_and_exact(dataset):
    did=dataset['id'];a=add(did);b=add(did,key='other');pids=[r['id'] for r in draft.sheet(did)['rows'][:2]]
    jobs=[]
    for col,pid in ((a,pids[0]),(b,pids[1])):
        proposal=draft.propose(did,'执行',[{'kind':'execute','column_ids':[col['id']],'product_ids':[pid]}]);job=draft.confirm(did,proposal['id'])[0];jobs.append(job)
        with Session.begin() as session:
            saved=require(session,BatchJob,job['id']);saved.status='partial';saved.errors={pid+':'+col['id']:'格式失败'}
    scope=task_control.create_repair_scope(did,[j['id'] for j in jobs])
    assert scope['cell_count']==2 and scope['column_count']==2
    assert {x['rerun'] for x in scope['summary']}=={1}
    assert all(x['human_values_protected'] for x in scope['summary'])
    proposal=task_control.repair(did,repair_scope_id=scope['id'])
    repaired=draft.confirm(did,proposal['id'])[0]
    assert set(repaired['spec']['expected'])==set(scope['tokens'])
    with Session() as session:
        assert all(require(session,BatchJob,j['id']).status=='resolved' for j in jobs)


def test_manual_correction_settles_previous_failed_job(dataset):
    did=dataset['id'];col=add(did);pid=draft.sheet(did)['rows'][0]['id']
    proposal=draft.propose(did,'执行',[{'kind':'execute','column_ids':[col['id']],'product_ids':[pid]}])
    job=draft.confirm(did,proposal['id'])[0];token=pid+':'+col['id']
    with Session.begin() as session:
        saved=require(session,BatchJob,job['id']);saved.status='partial';saved.errors={token:'格式失败'}
    draft.correct(did,pid,col['id'],'2件套')
    with Session() as session:
        saved=require(session,BatchJob,job['id'])
        assert saved.status=='resolved' and token in saved.completed and token not in saved.errors


def test_changed_repair_scope_is_rejected_at_confirmation(dataset):
    did=dataset['id'];col=add(did);pid=draft.sheet(did)['rows'][0]['id']
    original=draft.propose(did,'执行',[{'kind':'execute','column_ids':[col['id']],'product_ids':[pid]}])
    job=draft.confirm(did,original['id'])[0];token=pid+':'+col['id']
    with Session.begin() as session:
        saved=require(session,BatchJob,job['id']);saved.status='partial';saved.errors={token:'格式失败'}
    scope=task_control.create_repair_scope(did,[job['id']])
    proposal=task_control.repair(did,repair_scope_id=scope['id'])
    draft.correct(did,pid,col['id'],'人工值')
    with pytest.raises(ValueError,match='修复范围已变化'):
        draft.confirm(did,proposal['id'])


def test_semantic_repair_expands_to_full_column_with_zero_model_estimate(dataset):
    did=dataset['id'];col=add(did);rows=draft.sheet(did)['rows'];pid=rows[0]['id']
    original=draft.propose(did,'执行',[{'kind':'execute','column_ids':[col['id']],'product_ids':[pid]}])
    job=draft.confirm(did,original['id'])[0];token=pid+':'+col['id']
    with Session.begin() as session:
        saved=require(session,BatchJob,job['id']);saved.status='partial';saved.errors={token:'格式失败'}
    scope=task_control.create_repair_scope(did,[job['id']])
    fixed={**col['definition'],'result_shape':'scalar_open','options':[],'method':'text_rule','fields':['sku'],'parser':{'operation':'size_value','field':'sku','unknown':None}}
    proposal=task_control.repair(did,definitions={col['id']:fixed},repair_scope_id=scope['id'])
    execute=proposal['changes'][-1];summary=execute['repair_summary'][0]
    assert execute['actual_scope_computed'] is True
    assert set(execute['target_product_ids'][col['id']])=={row['id'] for row in rows}
    assert summary['rerun']==len(rows) and summary['estimated_requests']==0 and summary['preserved_success']==0


def test_visual_tagging_is_disabled_by_default(dataset,monkeypatch):
    monkeypatch.delenv('TAG_ENABLE_VISUAL_TAGGING',raising=False)
    with pytest.raises(ValueError,match='暂未开放'):
        add(dataset['id'],'visual',source='image')


def test_beta_health_exposes_capabilities_and_has_no_online_update_route(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import app
    monkeypatch.delenv('TAG_ENABLE_VISUAL_TAGGING',raising=False)
    with TestClient(app) as client:
        health=client.get('/api/health').json()
        assert health['capabilities']=={'visual_tagging':False,'visual_tagging_status':'disabled_for_beta','online_updates':False}
        assert client.get('/api/updates').status_code==404
