import json

from backend import batch,draft
from backend.db import Session,BatchJob,require
from test_draft import add,run


def test_only_invalid_visual_candidate_gets_one_format_repair(dataset,monkeypatch):
    did=dataset['id'];column=add(did,source='image',options=['花卉','条纹'],cardinality='multiple',result_shape='multi_enum')
    pids=[row['id'] for row in draft.sheet(did)['rows'][:2]];requests=[]
    monkeypatch.setattr(batch,'observation',lambda asset,*args:{'observation':'主图可见图案','asset_id':asset['id']})
    def response(model,messages,*args):
        inputs=json.loads(messages[-1]['content']);requests.append([item['product_id'] for item in inputs])
        if len(requests)==1:
            return {'results':[{'product_id':pids[0],'normalized_value':['花卉'],'evidence':'花朵','attributes':[{'attribute':'花卉','value':True,'evidence':'花朵'}]},
                               {'product_id':pids[1],'normalized_value':['条纹'],'evidence':'条纹','attributes':[{'attribute':'条纹','value':False,'evidence':'自相矛盾'}]}]}
        return {'results':[{'product_id':pids[1],'normalized_value':['条纹'],'evidence':'主图有条纹','attributes':[{'attribute':'条纹','value':True,'evidence':'主图有条纹'}]}]}
    monkeypatch.setattr(batch,'request_json',response)
    proposal=draft.propose(did,'视觉格式修复',[{'kind':'execute','column_ids':[column['id']],'product_ids':pids}])
    result=run(draft.confirm(did,proposal['id'])[0])
    assert result['status']=='completed' and requests==[pids,[pids[1]]]
    assert result['metrics']['format_repair_requests']==1
    with Session() as session:assert not require(session,BatchJob,result['id']).errors
