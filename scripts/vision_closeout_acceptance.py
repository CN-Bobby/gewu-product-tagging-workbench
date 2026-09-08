"""Three actual swimsuit rows, first/repeat vision and attributable research in a fresh DB."""
import json,os,sys,threading,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['TAG_STORAGE']=str(ROOT/'storage/release-verification'/('vision-'+uuid.uuid4().hex))
from backend.db import *
from backend import importer,draft,batch,knowledge,worksheet_query
from backend.harness import VISION_MODEL
init_db()
ds=importer.import_file((ROOT/'storage/originals/0cd683e717cf4910a285cf48a5fa9535.xlsx').read_bytes(),'泳衣352行隔离语义验收.xlsx')
rows=draft.sheet(ds['id'])['rows'];results=[]
requirements='识别泳衣各可见部件的真实结构和印花。区分分体坦基尼/连体、裙摆/短裤、花卉/叶片/几何纹样；观察领口、肩带及可见背部。必须说明部件和位置，看不到的背面不猜测，不把文字款名当图像事实。'
for index in [0,50,150]:
    row=rows[index];asset=next(a for a in row['assets'] if a.get('path'))
    metrics={};lock=threading.Lock()
    try:
        first=batch.observation(asset,VISION_MODEL,requirements,False,metrics,lock)
        repeat=batch.observation(asset,VISION_MODEL,requirements,False,metrics,lock)
        assert repeat['cached'] and metrics.get('vision_requests')==1
        results.append({'source_row':row['row'],'anchor':row['anchor'],'image':asset['path'],'first':first,'repeat':repeat,'metrics':metrics})
        print(json.dumps({'row':row['row'],'status':'observed','metrics':metrics},ensure_ascii=True),flush=True)
    except Exception as e:results.append({'source_row':row['row'],'error':str(e)})
report={'dataset_id':ds['id'],'samples':results,'profile':worksheet_query.profile(ds['id']),
        'knowledge':knowledge.research(ds['id'],query='tankini'),
        'invalid_source':knowledge.research(ds['id'],urls=['https://127.0.0.1/']),
        'semantic_review':'pending visual inspection; selected sample coverage only'}
(STORAGE/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(STORAGE/'report.json')},ensure_ascii=True),flush=True)
