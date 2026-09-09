import threading

from backend import batch,draft,worksheet_query
from backend.db import Asset,Session
from backend.image_inputs import primary_asset,selection


def add_supplemental(dataset):
    row=draft.sheet(dataset['id'])['rows'][0]
    original=row['assets'][0]
    with Session.begin() as session:
        session.add(Asset(product_id=row['id'],source='upload',path=original['path'],sha256=original['sha256']+'supplemental',image_type='supplemental'))
    return row['id'],original


def test_spreadsheet_main_image_wins_over_later_supplemental(dataset):
    pid,original=add_supplemental(dataset)
    row=next(r for r in draft.sheet(dataset['id'])['rows'] if r['id']==pid)
    chosen=selection(row)
    assert primary_asset(row)['id']==original['id']
    assert chosen['input_policy']=='single_main_image_v1'
    assert chosen['available_local_images']==2 and chosen['supplemental_images_ignored']==1


def test_representative_overview_groups_six_main_images(dataset,monkeypatch):
    calls=[]
    def compare(rows,model,goal,refresh,metrics,lock):
        calls.append([r['id'] for r in rows])
        metrics['vision_requests']=metrics.get('vision_requests',0)+1
        return {'products':[{'product_id':r['id'],'observation':'主图可见','features':['轮廓'],'unknown':[]} for r in rows],
                'patterns':['轮廓存在差异'],'coverage_note':'仅覆盖本组主图','cached':False}
    monkeypatch.setattr(batch,'calibration_group',compare)
    result=worksheet_query.visual_overview(dataset['id'],'比较款式轮廓',10)
    assert [len(x) for x in calls]==[6,4]
    assert result['input_policy']=='single_main_image_v1' and result['group_size']==6
    assert result['sample_count']==10 and result['successful']==10
    assert worksheet_query.profile(dataset['id'])['latest_visual_overview']['overview_id']==result['overview_id']


def test_representative_overview_bisects_failed_group(dataset,monkeypatch):
    calls=[]
    def compare(rows,model,goal,refresh,metrics,lock):
        calls.append(len(rows))
        if len(rows)>1:raise ValueError('组输出不完整')
        row=rows[0]
        return {'products':[{'product_id':row['id'],'observation':'单件恢复','features':['轮廓'],'unknown':[]}],
                'patterns':[],'coverage_note':'单件恢复','cached':False}
    monkeypatch.setattr(batch,'calibration_group',compare)
    result=worksheet_query.visual_overview(dataset['id'],'检查失败拆分',3)
    assert calls==[3,1,2,1,1] and result['successful']==3
    assert not [g for g in result['groups'] if g['status']=='failed']


def test_calibration_group_cache_uses_one_main_image_per_product(dataset,monkeypatch):
    rows=draft.sheet(dataset['id'])['rows'][:2];metrics={};lock=threading.Lock();seen=[]
    monkeypatch.setattr(batch,'request_json',lambda model,messages,*args:(seen.append(messages) or {
        'products':[{'product_id':r['id'],'observation':'主图可见','features':['结构'],'unknown':[]} for r in rows],
        'patterns':['结构差异'],'coverage_note':'只覆盖本组主图'}))
    first=batch.calibration_group(rows,'test/vision','比较结构',False,metrics,lock)
    second=batch.calibration_group(rows,'test/vision','比较结构',False,metrics,lock)
    assert not first['cached'] and second['cached'] and len(seen)==1
    assert metrics['vision_requests']==1 and metrics['main_images_compared']==2 and metrics['cache_hits']==1
