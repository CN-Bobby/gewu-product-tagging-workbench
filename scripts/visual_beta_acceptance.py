"""Real isolated acceptance for beta main-image and text+image tagging."""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
RUN=ROOT/'storage'/'release-verification'/('visual-beta-'+uuid.uuid4().hex)
os.environ['TAG_STORAGE']=str(RUN)

from backend.db import BatchJob,Session,init_db,obj,require
from backend import batch,draft,importer,worksheet_query
from backend.model_settings import defaults


def run_job(job):
    batch.active[job['id']]=threading.Event()
    batch.execute(job['id'])
    with Session() as session:return obj(require(session,BatchJob,job['id']))


def main():
    init_db()
    source=ROOT/'storage'/'originals'/'0cd683e717cf4910a285cf48a5fa9535.xlsx'
    dataset=importer.import_file(source.read_bytes(),'泳衣352行视觉Beta隔离验收.xlsx')
    rows=draft.sheet(dataset['id'])['rows']
    goal='比较泳衣主图中上衣长短、领口、肩带、下装结构以及花卉、叶片、几何、纯色等图案边界；看不到背面时保留未知'
    overview=worksheet_query.visual_overview(dataset['id'],goal,6)
    repeated=worksheet_query.visual_overview(dataset['id'],goal,6)
    if overview['successful']!=6 or overview['metrics'].get('vision_requests')!=1:
        raise AssertionError('首次六件同批主图比较未按一次视觉请求完成')
    if repeated['metrics'].get('vision_requests',0)!=0 or repeated['metrics'].get('cache_hits')!=1:
        raise AssertionError('重复主图比较未命中校准缓存')
    chosen=[row['id'] for row in rows[:5]]
    changes=[
        {'kind':'add','definition':{
            'key':'bottom_style_beta','name':'下装款式','type':'FACT','source':'image','result_shape':'scalar_enum','cardinality':'single',
            'definition':'只根据商品主图直接可见的下装轮廓，区分三角裤、平角短裤、裙式下装或其他；被遮挡或无法确认时留空。',
            'options':['三角裤','平角短裤','裙式下装','其他'],
            'judgment':{'target':'主图直接可见的下装款式','fields':[],'include':'轮廓直接可见且符合候选定义','exclude':'不得根据标题或未展示背面猜测','synonyms':{},'conflict':'保留图文冲突，等待确认','unknown':'下装被遮挡或轮廓不足时为null','examples':[]}}},
        {'kind':'add','definition':{
            'key':'pattern_elements_beta','name':'主图图案元素','type':'FACT','source':'both','result_shape':'multi_enum','cardinality':'multiple','fields':['sku','title','parameters'],
            'definition':'联合文字定位商品，但只用主图像素确认实际可见图案元素；允许多个明确命中，营销色名不能替代图像证据。',
            'options':['纯色','花卉','叶片','几何','条纹','波点','角色／动物','其他'],
            'judgment':{'target':'主图可见图案元素','fields':['sku','title','parameters'],'include':'主图中可定位的明确元素','exclude':'不得只按SKU色名或标题款名命中','synonyms':{},'conflict':'图片与文字冲突时以图片事实为观察并记录冲突','unknown':'画面模糊或元素不可辨时为null','examples':[]}}},
        {'kind':'execute','targets':[{'column_id':'$bottom_style_beta','product_ids':chosen},{'column_id':'$pattern_elements_beta','product_ids':chosen}],
         'overwrite':False,'reuse_results':False,'refresh_images':False},
    ]
    proposal=draft.propose(dataset['id'],'视觉Beta真实隔离验收',changes)
    text_model,vision_model=defaults()
    jobs=draft.confirm(dataset['id'],proposal['id'],text_model=text_model,vision_model=vision_model)
    if len(jobs)!=1:raise AssertionError('两列应合并为一项精确范围任务')
    started=time.perf_counter();job=run_job(jobs[0]);elapsed=time.perf_counter()-started
    state=draft.state(dataset['id'])
    columns={c['definition']['key']:c for c in state['columns']}
    results=[]
    for row in state['rows'][:5]:
        results.append({'row':row['row'],'anchor':row['anchor'],'image':next(a['path'] for a in row['assets'] if a.get('source')=='excel' and a.get('path')),
                        '下装款式':row['cells'][columns['bottom_style_beta']['id']],
                        '主图图案元素':row['cells'][columns['pattern_elements_beta']['id']]})
    expected=10
    report={'status':job['status'],'dataset_id':dataset['id'],'source_file':str(source),'input_policy':'single_main_image_v1',
            'overview_first':overview,'overview_repeat':repeated,'job':{'id':job['id'],'status':job['status'],'metrics':job['metrics'],'errors':job['errors'],'expected_cells':expected,'completed_cells':len(job['completed']),'wall_seconds':elapsed},
            'results':results,'semantic_review':'pending human review against the five saved main images'}
    (RUN/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(RUN/'report.json'),'status':job['status'],'completed':len(job['completed']),'errors':len(job['errors']),'metrics':job['metrics']},ensure_ascii=True),flush=True)
    if job['status']!='completed' or len(job['completed'])!=expected or job['errors']:
        raise SystemExit(1)


if __name__=='__main__':main()
