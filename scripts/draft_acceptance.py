"""Explicit, isolated real-model acceptance. Does not alter the user's worksheet."""
import os,sys,json,time,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['TAG_STORAGE']=str(ROOT/'storage'/'draft-acceptance')
from sqlalchemy import select
from backend.db import *
from backend import draft,batch,importer,draft_export,conversation

def report(x):print(json.dumps(x,ensure_ascii=False),flush=True)
def add(did,d):
    p=draft.propose(did,'验收测试规则',[{'kind':'add','definition':d}]);draft.confirm(did,p['id'])
    return next(c for c in draft.sheet(did)['columns'] if c['key']==d['key'])
def execute(did,c,overwrite=False):
    p=draft.propose(did,'验收测试执行',[{'kind':'execute','column_ids':[c['id']],'overwrite':overwrite}]);j=draft.confirm(did,p['id'])[0]
    batch.active[j['id']]=threading.Event();batch.execute(j['id'])
    with Session() as s:j=obj(require(s,BatchJob,j['id']))
    report({'job':j['id'],'status':j['status'],'metrics':j['metrics'],'errors':j['errors']});return j

if __name__=='__main__':
    init_db()
    with Session() as s:ds=s.scalar(select(Dataset).order_by(Dataset.created_at))
    if ds:did=ds.id
    else:
        sample=ROOT/'tests'/'fixtures'/'sample_10_rows.xlsx'
        did=importer.import_file(sample.read_bytes(),sample.name)['id']
    report({'dataset_id':did,'storage':str(STORAGE)})
    cols={c['key']:c for c in draft.sheet(did)['columns']}
    pieces=cols.get('pieces') or add(did,{'key':'pieces','name':'套装件数','type':'FACT','source':'text','definition':'仅根据标题和卖点明确标注的2pcs、2 piece或3 piece判断套装件数；没有明确数量则未知。禁止从图片判断。','fields':['title','bullets'],'options':['2件套','3件套']})
    j=execute(did,pieces,True)
    color=cols.get('color') or add(did,{'key':'color','name':'主色系','type':'INTERPRETATION','source':'image','definition':'观察图片商品服装主体最大面积颜色，综合色组明确为多彩。仅按可见颜色归类。','options':['红色系','橙色系','黄色系','绿色系','蓝色系','紫色系','粉色系','黑白灰','多彩']})
    vj=execute(did,color,True);cached=execute(did,color,True)
    warmth=cols.get('warmth') or add(did,{'key':'warmth','name':'冷暖色','type':'INTERPRETATION','source':'text','definition':'按主色系映射冷暖色：红橙黄粉为暖，蓝绿紫为冷，黑白灰中性，多彩为混合。','options':['暖色','冷色','中性','混合'],'method':'mapping','depends_on':[color['id']],'mapping':{'红色系':'暖色','橙色系':'暖色','黄色系':'暖色','粉色系':'暖色','绿色系':'冷色','蓝色系':'冷色','紫色系':'冷色','黑白灰':'中性','多彩':'混合'}})
    pid=draft.sheet(did)['rows'][0]['id'];draft.correct(did,pid,color['id'],'蓝色系');v1=draft.save_version(did);old=draft.sheet(did,v1['id']);draft.correct(did,pid,color['id'],'红色系');v2=draft.save_version(did)
    assert old==draft.sheet(did,v1['id'])
    path=draft_export.export(did,[pieces['id'],color['id'],warmth['id']])
    import openpyxl
    wb=openpyxl.load_workbook(path);assert len(wb['Product_Result']._images)==10 and wb['Product_Result'].max_column==73
    result={'dataset_id':did,'text_job':j,'vision_job':vj,'cached_job':cached,'v1':v1,'v2':v2,'differences':draft.compare(did,v1['id'],v2['id']),'export':str(path),'semantic_review':'模型结果尚未人工复核','old_text_wall_seconds':342.0}
    (STORAGE/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');report({'report':str(STORAGE/'report.json')})
