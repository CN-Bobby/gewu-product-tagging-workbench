import os,sys,json,threading,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));os.environ['TAG_STORAGE']=str(ROOT/'storage'/'draft-acceptance')
from sqlalchemy import select
from backend.db import *
from backend import draft,batch
init_db()
old=sqlite3.connect(ROOT/'storage/workbench.db');old.row_factory=sqlite3.Row
run=old.execute('select * from analysis_task where id=?',('886b1429fdf94830a43e7cb1b1531b69',)).fetchone()
schema=old.execute('select * from tag_schema where id=?',(run['schema_id'],)).fetchone();d=json.loads(schema['definitions'])[0];old.close()
d['key']='same_model_pieces';d['options']=[draft.translated(x) for x in d['options']];d['fields']=['title','bullets','parameters']
with Session() as s:did=s.scalar(select(Dataset.id))
existing=next((c for c in draft.sheet(did)['columns'] if c['key']==d['key']),None)
if not existing:
 p=draft.propose(did,'同模型基准定义',[{'kind':'add','definition':d}]);draft.confirm(did,p['id'])
c=next(c for c in draft.sheet(did)['columns'] if c['key']==d['key'])
p=draft.propose(did,'同模型冷启动测试',[{'kind':'execute','column_ids':[c['id']],'overwrite':True}]);j=draft.confirm(did,p['id'],text_model=run['model'],vision_model=run['model'])[0]
batch.active[j['id']]=threading.Event();batch.execute(j['id'])
with Session() as s:j=obj(require(s,BatchJob,j['id']))
report={'baseline':'历史同机10行套装件数任务，非同期复跑；避免重新制造10个Reasonix会话','old_run_id':run['id'],'old_model':run['model'],'old_wall_seconds':342.0,'new_model':j['spec']['text_model'],'new_job':j,'reduction_percent':100*(1-j['metrics']['wall_seconds']/342.0),'result_cache_used':False}
(STORAGE/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='new_job'},ensure_ascii=False));print(json.dumps(j['metrics'],ensure_ascii=False));print(j['status'])
