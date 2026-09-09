import json
import pytest
from backend import batch,draft,execution_contract
from backend.db import Session,BatchJob,require
from test_draft import add,run


def test_confirmed_prompts_survive_later_code_changes(dataset,monkeypatch):
    did=dataset['id'];column=add(did,source='image');pid=draft.sheet(did)['rows'][0]['id']
    proposal=draft.propose(did,'冻结执行规则',[{'kind':'execute','column_ids':[column['id']],'product_ids':[pid]}])
    job=draft.confirm(did,proposal['id'])[0];frozen=job['spec']['execution_contract']
    monkeypatch.setattr(batch,'VISION_RULES','NEW IMAGE PROMPT MUST NOT LEAK')
    monkeypatch.setattr(execution_contract,'FILL_PREFIX','NEW FILL PROMPT MUST NOT LEAK')
    seen=[]
    def observe(*args):
        assert args[7]==frozen['vision_rules']
        return {'observation':'图中蓝色上衣'}
    def response(model,messages,*args):
        seen.append(messages[0]['content'])
        return {'results':[{'product_id':p['product_id'],'normalized_value':'2件套','evidence':'图中两件'} for p in json.loads(messages[-1]['content'])]}
    monkeypatch.setattr(batch,'observation',observe);monkeypatch.setattr(batch,'request_json',response)
    result=run(job);assert result['status']=='completed'
    assert frozen['fill_prefix'] in seen[0] and 'MUST NOT LEAK' not in seen[0]


def test_missing_legacy_prompt_requires_explicit_repair(dataset):
    with pytest.raises(ValueError,match='修复提案'):execution_contract.require_contract({})


def test_text_prompt_change_invalidates_cache():
    from backend.text_facts import fingerprint
    source={'title':'Two Piece swimsuit'};rules={'sha256':'same-skill'}
    assert fingerprint(source,'same-model',rules,'original')!=fingerprint(source,'same-model',rules,'changed')


def test_changed_provider_is_not_silently_used(dataset,monkeypatch):
    from backend import vision
    monkeypatch.setattr(vision,'provider_config',lambda _:('https://first.invalid','SECRET_NOT_STORED','model'))
    did=dataset['id'];column=add(did)
    p=draft.propose(did,'固定模型服务',[{'kind':'execute','column_ids':[column['id']]}])
    job=draft.confirm(did,p['id'])[0]
    assert 'SECRET_NOT_STORED' not in json.dumps(job['spec'])
    monkeypatch.setattr(vision,'provider_config',lambda _:('https://other.invalid','SECRET_NOT_STORED','model'))
    monkeypatch.setattr(batch,'request_json',lambda *args:pytest.fail('changed endpoint must not be called'))
    result=run(job)
    assert result['status']=='partial' and any('模型服务配置' in v for v in result['errors'].values())


def test_text_and_image_cache_include_provider_identity(dataset,monkeypatch):
    import threading
    from backend import vision,text_facts
    monkeypatch.setattr(vision,'provider_config',lambda _:('https://first.invalid','secret','model'))
    asset=draft.sheet(dataset['id'])['rows'][0]['assets'][0];metrics={};lock=threading.Lock()
    monkeypatch.setattr(batch,'request_json',lambda *args:{'observation':'清晰的商品图'})
    first=text_facts.fingerprint({'title':'2pcs'},'same-ref',{'sha256':'skill'})
    batch.observation(asset,'same-ref','颜色',False,metrics,lock)
    assert batch.observation(asset,'same-ref','颜色',False,metrics,lock)['cached']
    monkeypatch.setattr(vision,'provider_config',lambda _:('https://second.invalid','secret','model'))
    assert text_facts.fingerprint({'title':'2pcs'},'same-ref',{'sha256':'skill'})!=first
    assert not batch.observation(asset,'same-ref','颜色',False,metrics,lock)['cached']
    assert metrics['vision_requests']==2 and metrics['cache_hits']==1
