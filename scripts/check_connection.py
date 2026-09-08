"""Small, explicit integration probe; no model call unless --prompt is supplied."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.db import init_db
from backend.harness import ACP, status, DEFAULT_MODEL

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('dataset_id');p.add_argument('--model',default=DEFAULT_MODEL);p.add_argument('--prompt')
    a=p.parse_args();init_db();print(json.dumps(status(),ensure_ascii=False),flush=True)
    c=ACP(a.dataset_id,a.model,on_event=lambda e:print(json.dumps(e,ensure_ascii=False),flush=True))
    try:
        print(json.dumps({'capabilities':c.capabilities['agentCapabilities']['promptCapabilities'],'session_id':c.session_id},ensure_ascii=False),flush=True)
        if a.prompt:print(c.prompt(a.prompt),flush=True)
    finally:c.close()
