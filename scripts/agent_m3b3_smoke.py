"""M3B-3 smoke against the real binary: one turn, streamed assistant text, then the turn outcome.

Opt-in on purpose: it launches the real Reasonix process and sends a real prompt, so it costs a
model call and must not run inside the pytest suite. Two rounds run in the same session.

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m3b3_smoke.py
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--storage',help='隔离数据目录；缺省新建系统临时目录，结束后删除')
parser.add_argument('--provider-home',help='模型服务配置目录；缺省沿用 TAG_PROVIDER_HOME')
parser.add_argument('--model',default='deepseek/deepseek-v4-flash')
parser.add_argument('--keep',action='store_true',help='保留临时目录以便排查')
args=parser.parse_args()

temporary=not args.storage
storage=Path(args.storage).resolve() if args.storage else Path(tempfile.mkdtemp(prefix='m3b3-smoke-'))
storage.mkdir(parents=True,exist_ok=True)
os.environ['TAG_STORAGE']=str(storage)
if args.provider_home:os.environ['TAG_PROVIDER_HOME']=args.provider_home
sys.path.insert(0,str(ROOT))

from backend import harness                                   # noqa: E402
from backend.runtime_isolation import environment             # noqa: E402
from backend.agent import protocol as agent_protocol          # noqa: E402
from backend.agent.runtime import Runtime                      # noqa: E402
from backend.agent.reasonix import ReasonixAdapter,ReasonixLaunchConfig  # noqa: E402

outbound=[]
_original_send=Runtime.send


def _recording_send(self,message):
    try:outbound.append(agent_protocol.line(message).rstrip('\n'))
    except Exception:pass
    return _original_send(self,message)


Runtime.send=_recording_send

report={'storage':str(storage),'provider_home':os.environ.get('TAG_PROVIDER_HOME'),'steps':[]}


def step(name,ok,detail=None):
    report['steps'].append({'name':name,'ok':bool(ok),'detail':detail})
    return bool(ok)


def live_readers():
    return [t.name for t in threading.enumerate() if t.name.startswith('reasonix-acp')]


def methods_since(index):
    names=[]
    for line in outbound[index:]:
        try:names.append(json.loads(line).get('method'))
        except json.JSONDecodeError:pass
    return names


cwd=storage/'cwd';cwd.mkdir(parents=True,exist_ok=True)
(cwd/'PRODUCT_ANALYST_AGENT.md').write_text(
    'Reply literally and briefly. Never call tools, never explain, never add punctuation.\n',encoding='utf-8')
(cwd/'reasonix.toml').write_text(
    '[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\nplanner_model = ""\n'
    '\n[environment]\nenabled = false\n'
    '\n[tools]\nenabled = []\n'
    '\n[permissions]\nmode = "deny"\nallow = []\n'
    'deny = ["bash","write","edit","read_file","ls","glob","grep","task","run_skill"]\n',encoding='utf-8')

adapter=None
try:
    isolated=environment('m3b3smoke',None,{})
    events=[]
    def sink(event):
        events.append({'at':time.perf_counter(),'event':event})
    adapter=ReasonixAdapter(ReasonixLaunchConfig(executable=harness.executable(),model=args.model,
        cwd=str(cwd),env=isolated,runtime_key='m3b3-smoke'))
    adapter.bind(sink)
    opened=time.perf_counter()
    adapter.start()
    report['handshake_seconds']=round(time.perf_counter()-opened,3)
    session_id=adapter.status()['remote_session_id']
    pid=adapter.status()['pid']
    step('1 启动并创建会话',adapter.status()['ready'] and bool(session_id),
         {'pid':pid,'session_id':session_id})

    def run_turn(index,text,label):
        mark=len(outbound)
        before=len(events)
        started=time.perf_counter()
        adapter.send(text)
        seconds=round(time.perf_counter()-started,3)
        turn=events[before:]
        deltas=[e['event'].get('text') for e in turn if e['event'].get('type')=='assistant_text_delta']
        settled=[e for e in turn if e['event'].get('type')=='turn_settled']
        kinds=[e['event'].get('type') for e in turn]
        order_ok=(kinds and kinds[-1]=='turn_settled'
                  and kinds.index('turn_settled')>max(
                      i for i,k in enumerate(kinds) if k=='assistant_text_delta'))
        methods=methods_since(mark)
        return {'label':label,'text':text,'seconds':seconds,'methods':methods,
                'delta_count':len(deltas),'deltas':deltas[:20],'joined':''.join(deltas),
                'turn_settled_count':len(settled),
                'stop_reason':(settled[0]['event'].get('stop_reason') if settled else None),
                'turn_settled_after_last_delta':order_ok,'kinds':kinds}

    first=run_turn(1,'Reply with exactly: M3B3_OK','first')
    step('2 首轮请求方法',first['methods']==['session/prompt'],{'methods':first['methods']})
    step('3 收到真实 assistant 文本',first['delta_count']>=1,
         {'delta_count':first['delta_count'],'deltas':first['deltas'],'joined':first['joined']})
    step('4 拼接文本',True,{'joined':first['joined'],'equals_expected':first['joined']=='M3B3_OK'})
    step('5 turn_settled 晚于最后一个 delta',
         first['turn_settled_after_last_delta'] and first['turn_settled_count']==1)
    step('6 stopReason 为真实值',bool(first['stop_reason']),{'stop_reason':first['stop_reason']})
    state=adapter.status()
    step('7 首轮结束后会话仍可用',
         state['ready'] is True and state['alive'] is True and state['remote_session_id']==session_id,
         {'ready':state['ready'],'alive':state['alive'],
          'session_id_unchanged':state['remote_session_id']==session_id})

    second=run_turn(2,'Reply with exactly: SECOND_OK','second')
    forbidden={'session/new','session/resume','session/load'}
    step('8 第二轮仍是同一个 session/prompt',
         second['methods']==['session/prompt'] and not (forbidden & set(second['methods'])),
         {'methods':second['methods']})
    step('9 第二轮完成且 session id 不变',
         second['turn_settled_count']==1 and adapter.status()['remote_session_id']==session_id,
         {'joined':second['joined'],'stop_reason':second['stop_reason'],
          'session_id':adapter.status()['remote_session_id']})
    report['turns']=[first,second]

    closing=time.perf_counter()
    adapter.stop()
    after=adapter.diagnostics()
    deadline=time.time()+5
    while live_readers() and time.time()<deadline:time.sleep(.05)
    step('10 收尾无残留',
         adapter.request_methods()[-1:]==['session/close'] and not adapter.status()['alive']
         and not live_readers(),
         {'close_seconds':round(time.perf_counter()-closing,3),
          'exit':{k:v for k,v in (after['exit'] or {}).items() if k!='lifecycle'},
          'readers':live_readers()})
    step('11 全程无协议错误',not after['runtime']['protocol'],
         {'protocol_errors':after['runtime']['protocol']})
    step('12 未意外触发工具',not after['ignored_updates'].get('tool_call'),
         {'ignored_updates':after['ignored_updates']})
except Exception as error:
    report['error']={'type':type(error).__name__,'message':str(error)[:500],
                     'detail':getattr(error,'detail',None)}
finally:
    Runtime.send=_original_send
    if adapter is not None:
        try:adapter.stop()
        except Exception:pass

report['wire']=list(outbound)
report['ok']=bool(report['steps']) and all(item['ok'] for item in report['steps']) and 'error' not in report
print(json.dumps(report,ensure_ascii=False,indent=2))
if temporary and not args.keep:shutil.rmtree(storage,ignore_errors=True)
raise SystemExit(0 if report['ok'] else 1)
