"""M3B-1 smoke against the real binary: start -> initialize -> session/new -> status -> stop.

Opt-in on purpose: it launches the real Reasonix process, so it must not run inside the pytest
suite. No prompt is sent, so no model call is made and the cost is a session handshake only.
Everything runs in an isolated storage root and a temporary session workspace.

The frames are recorded where the adapter hands them to the transport, so the smoke can show
what was really sent instead of inferring it from the adapter's own intentions. The real binary
answering those exact frames is what proves they were the accepted shape.

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m3b1_smoke.py
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
storage=Path(args.storage).resolve() if args.storage else Path(tempfile.mkdtemp(prefix='m3b1-smoke-'))
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
    return [thread.name for thread in threading.enumerate() if thread.name.startswith('reasonix-acp')]


def sent_frame(method):
    for line in outbound:
        try:frame=json.loads(line)
        except json.JSONDecodeError:continue
        if frame.get('method')==method:return frame
    return None


cwd=storage/'cwd';cwd.mkdir(parents=True,exist_ok=True)
(cwd/'PRODUCT_ANALYST_AGENT.md').write_text('smoke only\n',encoding='utf-8')
(cwd/'reasonix.toml').write_text(
    '[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\nplanner_model = ""\n'
    '\n[environment]\nenabled = false\n\n[tools]\nenabled = []\n',encoding='utf-8')

adapter=None
try:
    opened=time.perf_counter()
    isolated=environment('m3b1smoke',None,{})
    executable=harness.executable()
    step('0 解析可执行文件',bool(executable),{'executable':executable})
    adapter=ReasonixAdapter(ReasonixLaunchConfig(executable=executable,model=args.model,
        cwd=str(cwd),env=isolated))
    step('0b 装配启动命令',adapter.launch_args()==['acp','--model',args.model],
         {'launch_args':adapter.launch_args(),'configured_cwd':str(cwd)})
    adapter.start()
    handshake_seconds=round(time.perf_counter()-opened,3)
    state=adapter.status()
    debug=adapter.diagnostics()
    completed=[event for event in debug['runtime']['lifecycle'] if event.get('type')=='request_completed']

    step('1 启动并完成握手',state['ready'] and state['alive'],
         {'total_seconds':handshake_seconds,'pid':state['pid']})
    step('2 请求顺序与各自耗时',adapter.request_methods()==['initialize','session/new'],
         {'requests':[{'method':event.get('method'),'seconds':event.get('seconds')} for event in completed]})
    step('3 获得 remote session id',bool(state['remote_session_id']),
         {'remote_session_id':state['remote_session_id']})
    step('4 无协议错误',not debug['runtime']['protocol'],
         {'protocol_errors':debug['runtime']['protocol']})

    initialize=sent_frame('initialize')
    created=sent_frame('session/new')
    step('5 实际 initialize frame',bool(initialize),{'frame':initialize})
    step('6 实际 session/new frame 与 cwd',
         bool(created) and created.get('params',{}).get('cwd')==str(cwd),
         {'frame':created,'expected_cwd':str(cwd),
          'cwd_absolute':Path(str(cwd)).is_absolute(),'cwd_exists':Path(str(cwd)).is_dir()})
    step('7 stderr',True,{'stderr':[item['line'].rstrip() for item in debug['runtime']['stderr'][-8:]]})

    closing=time.perf_counter()
    adapter.stop()
    after=adapter.diagnostics()
    final=adapter.status()
    step('8 session/close 与进程终止',
         adapter.request_methods()[-1:]==['session/close'] and final['alive'] is False,
         {'close_seconds':round(time.perf_counter()-closing,3),
          'status':{'pid':final['pid'],'alive':final['alive'],'ready':final['ready']},
          'diagnostics_exit':{key:value for key,value in (after['exit'] or {}).items() if key!='lifecycle'},
          'diagnostics_history_lifecycle_events':len((after['exit'] or {}).get('lifecycle') or [])})
    deadline=time.time()+5
    while live_readers() and time.time()<deadline:time.sleep(.05)
    step('9 reader 线程已退出',not live_readers(),{'readers':live_readers()})
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
