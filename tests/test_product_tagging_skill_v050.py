import json
import threading

from backend import batch,draft,tagging_rules,worksheet_query
from backend.db import Session,WorkbenchMeta


def test_manifest_routes_only_relevant_modules():
    bundle=tagging_rules.freeze()
    definition={'key':'body_color','name':'杯身主色','definition':'判断水杯杯身主色，不含杯盖和颜色缩略图','source':'image','options':['黑色','粉色'],'dimension':'appearance','product_category':'drinkware','product_forms':['multi_variant']}
    profile=tagging_rules.compile_runtime_profile(definition,'execution',bundle)
    assert profile['profile_version']=='runtime-profile-v1'
    assert {'category.drinkware','dimension.appearance','form.multi_variant','mode.execution'}<=set(profile['module_ids'])
    assert 'category.apparel' not in profile['module_ids'] and 'category.electronics' not in profile['module_ids']
    prompt=tagging_rules.runtime_prompt({**definition,'runtime_profile':profile},bundle)
    assert '杯身' in prompt and '服装主体' not in prompt


def test_confirm_freezes_compiled_profile(dataset):
    did=dataset['id'];definition={'key':'mount_form','name':'固定结构','type':'FACT','source':'image','definition':'判断车载手机支架的固定与连接结构','options':[],'dimension':'structure','product_category':'electronics','product_forms':['mounting_system']}
    proposal=draft.propose(did,'新增结构标签',[{'kind':'add','definition':definition}]);draft.confirm(did,proposal['id'])
    col=next(c for c in draft.sheet(did)['columns'] if c['key']=='mount_form');profile=col['definition']['runtime_profile']
    assert col['definition']['skill']['version']=='0.5.0' and profile['expansion_policy']=='end_of_task_suggestion'
    assert {'dimension.structure_material','category.electronics','form.mounting_system'}<=set(profile['module_ids'])


def test_full_scan_requires_separate_confirmation(dataset,monkeypatch):
    monkeypatch.setattr(batch,'calibration_group',lambda *args:(_ for _ in ()).throw(AssertionError('不得提前看图')))
    result=worksheet_query.visual_overview(dataset['id'],'形成颜色标签定义',24,full_scan=True,confirmed_full_scan=False)
    assert result['status']=='confirmation_required'
    assert result['full_scan_estimate']['estimated_vision_requests']>=1


def test_overview_returns_cross_group_visual_standard(dataset,monkeypatch):
    def compare(rows,model,goal,refresh,metrics,lock):
        return {'products':[{'product_id':r['id'],'observation':'杯身可见','features':['杯身主色'], 'unknown':[]} for r in rows],
                'patterns':['杯身与杯盖需分开'],'candidate_values':['黑色'],'boundaries':{'include':['杯身最大连续色'],'exclude':['杯盖']},'unresolved':[],
                'coverage_note':'仅覆盖本组','cached':False}
    monkeypatch.setattr(batch,'calibration_group',compare)
    result=worksheet_query.visual_overview(dataset['id'],'校准水杯杯身主色',10)
    standard=result['visual_standard']
    assert standard['scope']=='dataset_standard' and standard['sample_count']==10
    assert standard['candidate_values']==['黑色'] and standard['confirmed'] is False
    proposal=draft.propose(dataset['id'],'使用已看样本形成标签',[{'kind':'add','definition':{'key':'cup_color','name':'杯身主色','type':'FACT','source':'image','definition':'只判断杯身最大连续区域颜色','options':['黑色'],'visual_standard_id':result['overview_id']}}])
    draft.confirm(dataset['id'],proposal['id'])
    col=next(c for c in draft.sheet(dataset['id'])['columns'] if c['key']=='cup_color')
    assert col['definition']['visual_standard']['confirmed'] is True
    with Session() as session:assert session.get(WorkbenchMeta,result['overview_id']).data['visual_standard']['confirmed'] is True


def test_dense_crop_policy_is_versioned_and_cached(dataset,monkeypatch):
    row=draft.sheet(dataset['id'])['rows'][0];asset=row['assets'][0];seen=[]
    def response(model,messages,metrics,lock):
        seen.append(messages)
        return {'observation':'整图及四区已观察','entities':[],'relationships':[],'dimensions':{},'parts':[],'unknown':[],'conflicts':[]}
    monkeypatch.setattr(batch,'request_json',response)
    from backend.image_inputs import selection
    image_input=selection(row,'single_main_with_quadrant_crops_v1');metrics={};lock=threading.Lock()
    first=batch.observation(asset,'test/vision','观察密集吊坠主题',True,metrics,lock,rules=batch.VISION_RULES,image_input=image_input)
    assert first['input_policy']=='single_main_with_quadrant_crops_v1' and metrics['vision_images_sent']==5
    assert sum(item.get('type')=='image_url' for item in seen[0][1]['content'])==5


def test_screenshot_grid_policy_sends_whole_image_and_regions(dataset,monkeypatch):
    row=draft.sheet(dataset['id'])['rows'][0];asset=row['assets'][0];seen=[]
    monkeypatch.setattr(batch,'request_json',lambda model,messages,*args:(seen.append(messages) or {'observation':'整图与网格已观察'}))
    from backend.image_inputs import selection
    image_input=selection(row,'screenshot_grid_crops_v1');metrics={}
    result=batch.observation(asset,'test/vision','观察榜单截图中的商品卡片',True,metrics,threading.Lock(),rules=batch.VISION_RULES,image_input=image_input)
    assert result['crop_policy']=='adaptive_grid_v1' and metrics['vision_images_sent']>=3
    assert sum(item.get('type')=='image_url' for item in seen[0][1]['content'])==metrics['vision_images_sent']
