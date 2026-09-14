"""M3B-2 smoke against the real binary: resume a persisted session in a brand-new process.

Opt-in on purpose: it launches the real Reasonix process, so it must not run inside the pytest
suite. No prompt is sent, so no model call is made.

Three phases, and the first two are what the CLI actually allows for a session that never ran a
turn:

  A  create a session, note its id, tear the process down
  B  a brand-new process tries to resume that id and reports whatever the peer really says
  C  given a session that DOES have turns (--persisted-session), resume it for real and reload
     the extension surface, which is the happy path the adapter must support

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m3b2_smoke.py
  .venv\\Scripts\\python.exe scripts/agent_m3b2_smoke.py --persisted-session <dir> --persisted-session-id <id>
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
parser.add_argument('--persisted-session',help='含真实已持久会话的 sessions 目录（阶段 C）')
parser.add_argument('--persisted-session-id',help='阶段 C 要恢复的会话标识')
parser.add_argument('--keep',action='store_true',help='保留临时目录以便排查')
args=parser.parse_args()

temporary=not args.storage
storage=Path(args.storage).resolve() if args.storage else Path(tempfile.mkdtemp(prefix='m3b2-smoke-'))
storage.mkdir(parents=True,exist_ok=True)
os.environ['TAG_STORAGE']=str(storage)
if args.provider_home:os.environ['TAG_PROVIDER_HOME']=args.provider_home
sys.path.insert(0,str(ROOT))

from backend import harness                                   # noqa: E402
from backend.runtime_isolation import environment             # noqa: E402
from backend.agent import protocol as agent_protocol          # noqa: E402
from backend.agent.runtime import Runtime                      # noqa: E402
from backend.agent.reasonix import ReasonixAdapter,ReasonixLaunchConfig,ReasonixResumeState  # noqa: E402

RUNTIME_KEY='m3b2-smoke-key'
outbound=[]
_original_send=Runtime.send


def _recording_send(self,message):
    try:outbound.append(agent_protocol.line(message).rstrip('\n'))
    except Exception:pass
    return _original_send(self,message)


Runtime.send=_recording_send

report={'storage':str(storage),'provider_home':os.environ.get('TAG_PROVIDER_HOME'),'phases':[]}


def note(phase,name,ok,detail=None):
    report['phases'].append({'phase':phase,'step':name,'ok':bool(ok),'detail':detail})
    return bool(ok)


def live_readers():
    return [t.name for t in threading.enumerate() if t.name.startswith('reasonix-acp')]


def wire_since(index):
    frames=[]
    for line in outbound[index:]:
        try:frames.append(json.loads(line))
        except json.JSONDecodeError:pass
    return frames


def make_cwd(path):
    path.mkdir(parents=True,exist_ok=True)
    if not (path/'reasonix.toml').is_file():
        (path/'PRODUCT_ANALYST_AGENT.md').write_text('smoke only\n',encoding='utf-8')
        (path/'reasonix.toml').write_text(
            '[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\nplanner_model = ""\n'
            '\n[environment]\nenabled = false\n\n[tools]\nenabled = []\n',encoding='utf-8')
    return path


def build(cwd,resume=None,dataset='m3b2smoke'):
    isolated=environment(dataset,None,{})
    adapter=ReasonixAdapter(ReasonixLaunchConfig(executable=harness.executable(),model=args.model,
        cwd=str(cwd),env=isolated,runtime_key=RUNTIME_KEY,resume=resume))
    return adapter,isolated


adapter=None
try:
    cwd=make_cwd(storage/'cwd')

    # --- phase A: create a session ---------------------------------------
    mark=len(outbound)
    adapter,_=build(cwd)
    adapter.start()
    session_id=adapter.status()['remote_session_id']
    pid_a=adapter.status()['pid']
    frames=wire_since(mark)
    note('A','创建会话并握手',adapter.status()['ready'] and bool(session_id),
         {'pid':pid_a,'session_id':session_id,'methods':[f.get('method') for f in frames]})
    note('A','new 路径不含 resume/reload',
         [f.get('method') for f in frames]==['initialize','session/new'])
    adapter.stop()
    deadline=time.time()+5
    while live_readers() and time.time()<deadline:time.sleep(.05)
    note('A','进程与 reader 完全退出',
         not adapter.status()['alive'] and not live_readers(),
         {'exit':{k:v for k,v in (adapter.diagnostics()['exit'] or {}).items() if k!='lifecycle'}})
    adapter=None

    # --- phase B: a new process tries to resume it ------------------------
    mark=len(outbound)
    resume_error=None
    third=None
    try:
        third,_=build(cwd,resume=ReasonixResumeState(session_id=session_id,runtime_key=RUNTIME_KEY))
        third.start()
        pid_b=third.status()['pid']
        note('B','新进程恢复成功',True,{'pid':pid_b,'pid_differs':pid_b!=pid_a,
             'session_id':third.status()['remote_session_id']})
    except Exception as error:
        resume_error=str(error)[:300]
        pid_b=third.diagnostics()['exit']['pid'] if third is not None and third.diagnostics()['exit'] else None
        note('B','新进程恢复未成功（记录真实原因）',True,
             {'error':resume_error,'detail':getattr(error,'detail',None),'pid':pid_b,
              'pid_differs':pid_b!=pid_a,
              'methods':[f.get('method') for f in wire_since(mark)]})
    finally:
        if third is not None:
            methods=[f.get('method') for f in wire_since(mark)]
            note('B','失败后无 session/new 与 session/load',
                 'session/new' not in methods and 'session/load' not in methods,{'methods':methods})
            third.stop()
            deadline=time.time()+5
            while live_readers() and time.time()<deadline:time.sleep(.05)
            note('B','无残留进程或 reader',not third.status()['alive'] and not live_readers())
            third=None

    # --- phase C: resume a session that really has turns ------------------
    if args.persisted_session and args.persisted_session_id:
        source=Path(args.persisted_session)
        sid=args.persisted_session_id
        acp_file=source/(sid+'.acp.json')
        persisted_cwd=json.loads(acp_file.read_text(encoding='utf-8')).get('cwd') if acp_file.is_file() else None
        if not persisted_cwd or not Path(str(persisted_cwd)).is_dir():
            note('C','跳过：找不到可用会话工作目录',False,{'recorded_cwd':persisted_cwd})
        else:
            mark=len(outbound)
            fourth,isolated=build(Path(str(persisted_cwd)),
                                  resume=ReasonixResumeState(session_id=sid,runtime_key=RUNTIME_KEY),
                                  dataset='m3b2resume')
            home=Path(isolated['REASONIX_HOME'])/'sessions';home.mkdir(parents=True,exist_ok=True)
            copied=[]
            for path in source.iterdir():
                if not path.name.startswith(sid) or path.name.endswith(('.lock','.lease.json')):continue
                dest=home/path.name
                if dest.exists():continue
                if path.is_dir():shutil.copytree(path,dest,symlinks=True)
                else:shutil.copy2(path,dest)
                copied.append(path.name)
            try:
                fourth.start()
                frames=wire_since(mark)
                methods=[f.get('method') for f in frames]
                note('C','恢复真实持久会话并重载扩展',fourth.status()['ready'],
                     {'session_id':fourth.status()['remote_session_id'],'pid':fourth.status()['pid'],
                      'methods':methods,'reload_method':fourth.diagnostics()['reload_method'],
                      'copied_files':len(copied)})
                note('C','顺序为 initialize → session/resume → reloadExtensions',
                     methods==['initialize','session/resume','_reasonix.io/session/reloadExtensions'],
                     {'methods':methods})
                note('C','不含 session/new / session/load / session/prompt',
                     not {'session/new','session/load','session/prompt'}&set(methods))
                note('C','恢复后 identity 保持',fourth.status()['remote_session_id']==sid,
                     {'requested':sid,'reported':fourth.status()['remote_session_id']})
                note('C','resume wire frame',len(frames)>1,
                     {'resume_frame':frames[1] if len(frames)>1 else None})
            finally:
                fourth.stop()
                deadline=time.time()+5
                while live_readers() and time.time()<deadline:time.sleep(.05)
                note('C','无残留进程或 reader',not fourth.status()['alive'] and not live_readers())
except Exception as error:
    report['error']={'type':type(error).__name__,'message':str(error)[:500],
                     'detail':getattr(error,'detail',None)}
finally:
    Runtime.send=_original_send
    if adapter is not None:
        try:adapter.stop()
        except Exception:pass

report['wire']=list(outbound)
report['ok']=bool(report['phases']) and all(p['ok'] for p in report['phases']) and 'error' not in report
print(json.dumps(report,ensure_ascii=False,indent=2))
if temporary and not args.keep:shutil.rmtree(storage,ignore_errors=True)
raise SystemExit(0 if report['ok'] else 1)
