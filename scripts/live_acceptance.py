"""Run real Reasonix + vision on the provided sample in an isolated acceptance database.
This never approves business rules in the user's normal workspace.
"""
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['TAG_STORAGE']=str(ROOT/'storage'/'acceptance')
import json
from sqlalchemy import select
from backend.db import *
from backend import importer,service,jobs
from backend.harness import VISION_MODEL
from backend.exporter import export_run

if __name__=='__main__':
    init_db()
    with Session() as s:old=s.scalar(select(Run).order_by(Run.created_at.desc()))
    if old and old.kind=='sample':
        run=obj(old);dataset_id=old.dataset_id;schema_id=old.schema_id
    else:
        sample=ROOT/'tests'/'fixtures'/'sample_10_rows.xlsx'
        ds=importer.import_file(sample.read_bytes(),'自动验收_sample_10_rows.xlsx')
        sc=service.propose(ds['id'],'自动化功能验收用规则；不是用户确认的业务结论',[
            {'key':'color','name':'视觉颜色','type':'FACT','definition':'依据视觉工具返回的像素观察，列出服装可见的主要颜色组合；不把SKU色名当作图像事实','options':[],'source':'image'},
            {'key':'pattern','name':'图案','type':'FACT','definition':'依据视觉工具描述主要图案元素；纯色明确写纯色，无法观察则留空','options':[],'source':'image'},
            {'key':'pack_count','name':'声明件数','type':'FACT','definition':'只根据标题或五点明确写出的套装件数，未写明则留空','options':[],'source':'text'},
        ])
        service.approve_schema(sc['id'])
        run=service.create_run(ds['id'],sc['id'],'sample',VISION_MODEL)
        dataset_id=ds['id'];schema_id=sc['id']
    print(json.dumps({'dataset_id':dataset_id,'run_id':run['id'],'storage':str(STORAGE)},ensure_ascii=False),flush=True)
    jobs.active[run['id']]={'stop':False,'client':None}
    jobs._execute(run['id'])
    with Session() as s:r=require(s,Run,run['id']);print(json.dumps(obj(r),ensure_ascii=False),flush=True)
    if r.status!='completed':sys.exit(1)
    results=service.run_results(r.id)
    for t in results:service.review_tag(t['id'],note='自动化结构验收，仅测试审核机制；未经人工语义确认')
    service.approve_sample(r.id)
    full=service.create_run(dataset_id,schema_id,'full',VISION_MODEL)
    jobs.active[full['id']]={'stop':False,'client':None};jobs._execute(full['id'])
    target=export_run(full['id'])
    report={'dataset_id':dataset_id,'sample_run':run['id'],'full_run':full['id'],'products':len(results),'tag_values':sum(len(t['values']) for t in results),'export':str(target),'semantic_review':'NOT_HUMAN_REVIEWED','results':results}
    (STORAGE/'acceptance-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False),flush=True)
