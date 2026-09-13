"""M1 smoke test against the real Reasonix CLI: checklist items 7-11.

Opt-in on purpose: it needs network, model quota and authorized credentials, so it must not
run inside the pytest suite (CI has no model endpoints). It drives the real harness.ACP, which
exercises protocol + runtime + caller wiring together.

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m1_smoke.py --storage <dir containing workbench.db>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--storage',required=True,help='数据目录（需含 workbench.db 与数据集）')
parser.add_argument('--dataset',help='数据集 id，缺省时取库中第一个')
parser.add_argument('--model',default='deepseek/deepseek-v4-flash')
parser.add_argument('--prompt',default='不要调用任何工具，只回复 OK 两个字。')
args=parser.parse_args()

os.environ['TAG_STORAGE']=str(Path(args.storage).resolve())
sys.path.insert(0,str(ROOT))

report={'storage':os.environ['TAG_STORAGE'],'provider_home':os.environ.get('TAG_PROVIDER_HOME'),'checks':[]}
def check(name,ok,detail=None):report['checks'].append({'name':name,'ok':bool(ok),'detail':detail})

from backend.db import Session,select,Dataset
from backend import harness

did=args.dataset
if not did:
    with Session() as s:did=s.scalar(select(Dataset.id))
if not did:raise SystemExit('storage 中没有数据集，请用 --dataset 指定')

client=None
try:
    opened=time.perf_counter()
    client=harness.ACP(did,args.model,role='coordinator')
    check('7 Reasonix CLI 启动',client.proc.poll() is None,
        {'executable':harness.executable(),'pid':client.proc.pid,'seconds':round(time.perf_counter()-opened,2)})
    check('8 initialize',bool(client.capabilities),{'protocolVersion':client.capabilities.get('protocolVersion')})
    check('9 创建/恢复 session',bool(client.session_id),{'session_id':client.session_id})
    prompted=time.perf_counter()
    text=client.prompt(args.prompt)
    check('10 发送最简单 prompt',True,{'seconds':round(time.perf_counter()-prompted,2),'model':client.model})
    check('11 收到文本响应',bool(text.strip()),{'chars':len(text),'preview':text.strip()[:160]})
except Exception as error:
    report['error']={'type':type(error).__name__,'message':str(error)[:500]}
    if getattr(error,'technical',None):report['technical']=error.technical
finally:
    if client:
        try:client.close()
        except Exception:pass

report['ok']=bool(report['checks']) and all(item['ok'] for item in report['checks']) and 'error' not in report
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(0 if report['ok'] else 1)
