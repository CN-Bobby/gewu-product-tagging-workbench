"""M3B-6 smoke against the real binary: cancel keeps the session, stop ends everything.

Opt-in on purpose: it launches the real Reasonix process and sends real prompts, so it costs model
calls and must not run inside the pytest suite. Two probes:

  A  cancel a turn mid-stream, then use the SAME session for a second turn
  B  stop the runtime while a turn is in flight, and prove nothing survives it

No MCP tool is involved: tools are disabled in the generated config, so the probe only exercises
turn-level cancel and teardown, which is what M3B-6 implemented.

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m3b6_smoke.py
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
storage=Path(args.storage).resolve() if args.storage else Path(tempfile.mkdtemp(prefix='m3b6-smoke-'))
storage.mkdir(parents=True,exist_ok=True)
os.environ['TAG_STORAGE']=str(storage)
if args.provider_home:os.environ['TAG_PROVIDER_HOME']=args.provider_home
sys.path.insert(0,str(ROOT))

import psutil                                                 # noqa: E402
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

LONG_PROMPT=('请从 1 开始连续输出整数，一直输出到 400，每行一个数字，'
             '不要输出任何其他文字，不要解释，不要调用任何工具。')
SHORT_PROMPT='只回复 OK 两个字母，不要任何其他内容。'

report={'storage':str(storage),'provider_home':os.environ.get('TAG_PROVIDER_HOME'),'steps':[]}


def step(name,ok,detail=None):
    report['steps'].append({'name':name,'ok':bool(ok),'detail':detail})
    return bool(ok)


def live_readers():
    return [thread.name for thread in threading.enumerate() if thread.name.startswith('reasonix-acp')]


def methods_since(index):
    names=[]
    for line in outbound[index:]:
        try:names.append(json.loads(line).get('method'))
        except json.JSONDecodeError:pass
    return names


def wait_until(predicate,timeout):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if predicate():return True
        time.sleep(.02)
    return predicate()


def make_config(cwd,key):
    return ReasonixLaunchConfig(executable=harness.executable(),model=args.model,
        cwd=str(cwd),env=environment('m3b6smoke',None,{}),runtime_key=key)


def descendants(pid):
    """Every living process below pid, so an orphan can be named instead of merely suspected."""
    if pid is None:return []
    try:parent=psutil.Process(pid)
    except psutil.Error:return []
    children=parent.children(recursive=True)
    return [{'pid':child.pid,'name':child.name()} for child in children]


cwd=storage/'cwd';cwd.mkdir(parents=True,exist_ok=True)
(cwd/'PRODUCT_ANALYST_AGENT.md').write_text(
    'Follow the user instruction literally. Never call tools, never explain, never add anything.\n',
    encoding='utf-8')
(cwd/'reasonix.toml').write_text(
    '[agent]\nsystem_prompt_file = "PRODUCT_ANALYST_AGENT.md"\nplanner_model = ""\n'
    '\n[environment]\nenabled = false\n'
    '\n[tools]\nenabled = []\n'
    '\n[permissions]\nmode = "deny"\nallow = []\n'
    'deny = ["bash","write","edit","read_file","ls","glob","grep","task","run_skill"]\n',encoding='utf-8')


class Turn:
    """One turn on its own thread, so the probe can cancel or stop it while it is still running."""

    def __init__(self,adapter,text,events):
        self.adapter=adapter;self.text=text;self.events=events
        self.error=None;self.result=None;self.finished=False
        self._thread=threading.Thread(target=self._drive,daemon=True)

    def _drive(self):
        try:self.result=self.adapter.send(self.text)
        except Exception as error:self.error=error
        finally:self.finished=True

    def start(self):
        self._thread.start()
        return self

    def wait_started(self,timeout=30):
        """Wait for the first streamed delta: the turn is really producing before we interrupt it."""
        return wait_until(lambda:any(event.get('type')=='assistant_text_delta' for event in self.events),
                          timeout)

    def join(self,timeout=90):
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def outcome(self):
        return {'error_type':type(self.error).__name__ if self.error else None,
                'error_message':str(self.error)[:200] if self.error else None,
                'fatal':getattr(self.error,'fatal',None) if self.error else None,
                'returned_cleanly':self.error is None and self.finished,
                'finished':self.finished}


def events_of(turn,turn_index):
    return turn.events[turn_index:]


# --- Probe A: cancel keeps the session ------------------------------------

def probe_a():
    adapter=None
    try:
        events=[]
        adapter=ReasonixAdapter(make_config(cwd,'m3b6-probe-a'))
        adapter.bind(events.append)
        adapter.start()
        session_id=adapter.status()['remote_session_id']
        pid=adapter.status()['pid']
        step('A1 启动并创建会话',adapter.status()['ready'] and bool(session_id),
             {'pid':pid,'session_id':session_id})

        mark=len(events);wire_mark=len(outbound)
        first=Turn(adapter,LONG_PROMPT,events).start()
        streamed=first.wait_started(30)
        before_cancel=len([e for e in events if e.get('type')=='assistant_text_delta'])
        time.sleep(0.4)                                       # let it be genuinely mid-answer
        cancel_return=adapter.cancel()
        cancel_alive=adapter.status()['alive']
        cancel_reader=adapter.diagnostics()['reader_alive']
        joined=first.join(90)
        turn=events_of(first,mark)
        kinds=[event.get('type') for event in turn]
        outcome=first.outcome()
        step('A2 回合在流式输出中被取消',streamed and cancel_return is True,
             {'streamed_before_cancel':streamed,'deltas_before_cancel':before_cancel,
              'cancel_returned':cancel_return})
        step('A3 取消后 send 有界退出且映射为 CancelledError',joined and outcome['error_type']=='CancelledError',
             dict(outcome,joined=joined))
        step('A4 取消只丢弃回合，运行时与 reader 都还在',
             cancel_alive is True and cancel_reader is True
             and adapter.status()['alive'] is True and adapter.diagnostics()['reader_alive'] is True
             and adapter.status()['ready'] is True,
             {'alive':adapter.status()['alive'],'reader_alive':adapter.diagnostics()['reader_alive'],
              'ready':adapter.status()['ready']})
        methods=methods_since(wire_mark)
        step('A5 取消没有重建会话',adapter.status()['remote_session_id']==session_id
             and 'session/new' not in methods and 'session/resume' not in methods
             and 'session/close' not in methods,
             {'session_id':adapter.status()['remote_session_id'],'session_id_unchanged':
              adapter.status()['remote_session_id']==session_id,
              'cancel_notified':'session/cancel' in methods,'methods':methods})

        mark2=len(events);wire_mark2=len(outbound)
        second=Turn(adapter,SHORT_PROMPT,events).start()
        closed=second.join(120)
        turn2=events_of(second,mark2)
        settled=[e for e in turn2 if e.get('type')=='turn_settled']
        methods2=methods_since(wire_mark2)
        step('A6 同一个 adapter / 同一个 session 的第二轮成功',
             closed and second.outcome()['returned_cleanly'] and settled
             and settled[-1].get('stop_reason')=='end_turn'
             and adapter.status()['remote_session_id']==session_id,
             {'joined':closed,'outcome':second.outcome(),
              'stop_reason':(settled[-1].get('stop_reason') if settled else None),
              'session_id_unchanged':adapter.status()['remote_session_id']==session_id,
              'methods':methods2})
        step('A7 第二轮没有重新建会话',not ({'session/new','session/resume'} & set(methods2)),
             {'methods':methods2})

        closing=time.perf_counter()
        adapter.stop()
        close_seconds=round(time.perf_counter()-closing,3)
        after=adapter.diagnostics()
        wait_until(lambda:not live_readers(),5)
        step('A8 stop 结束运行时且无残留',
             adapter.status()['alive'] is False and after['exit']['alive'] is False
             and not live_readers() and adapter.request_methods()[-1:]==['session/close'],
             {'close_seconds':close_seconds,
              'exit':{k:v for k,v in (after['exit'] or {}).items() if k!='lifecycle'},
              'readers':live_readers(),'alive':adapter.status()['alive']})
        step('A9 全程无协议错误',not after['runtime']['protocol'],
             {'protocol_errors':after['runtime']['protocol']})
        report['probe_a']={'session_id':session_id,'cancel_returned':cancel_return,
                           'first_turn':outcome,'second_turn':second.outcome(),
                           'kinds_first_turn':kinds,
                           'stop_reason_second':(settled[-1].get('stop_reason') if settled else None)}
    except Exception as error:
        report.setdefault('errors',{})['probe_a']={'type':type(error).__name__,
                                                   'message':str(error)[:500]}
    finally:
        if adapter is not None:
            try:adapter.stop()
            except Exception:pass


# --- Probe B: stop tears everything down ----------------------------------

def probe_b():
    adapter=None
    try:
        events=[]
        adapter=ReasonixAdapter(make_config(cwd,'m3b6-probe-b'))
        adapter.bind(events.append)
        adapter.start()
        session_id=adapter.status()['remote_session_id']
        pid=adapter.status()['pid']
        turn=Turn(adapter,LONG_PROMPT,events).start()
        streamed=turn.wait_started(30)
        runtime=adapter._runtime                                 # probe-only introspection
        pending_rpc=len(runtime.pending) if runtime is not None else None
        children_before=descendants(pid)
        step('B1 真实回合已在运行',streamed and adapter.diagnostics()['in_flight'] is True,
             {'streamed':streamed,'pid':pid,'session_id':session_id,
              'pending_rpc':pending_rpc,'children_before':children_before})

        outcome_holder={}
        def stopper():
            started=time.perf_counter()
            try:adapter.stop()
            except Exception as error:outcome_holder['error']=error
            outcome_holder['seconds']=round(time.perf_counter()-started,3)
        thread=threading.Thread(target=stopper,daemon=True)
        thread.start()
        thread.join(60)
        stop_bounded=not thread.is_alive()
        turn_bounded=turn.join(60)
        outcome=turn.outcome()
        state=adapter.status()
        after=adapter.diagnostics()
        wait_until(lambda:not live_readers(),5)
        children_after=descendants(pid)
        alive_children=[child for child in children_after
                        if psutil.pid_exists(child['pid'])]
        process_gone=not psutil.pid_exists(pid)
        step('B2 stop 有界完成',stop_bounded,
             {'stop_seconds':outcome_holder.get('seconds'),'stop_error':
              type(outcome_holder['error']).__name__ if outcome_holder.get('error') else None})
        step('B3 send 有界退出，且不是被误报为意外失去运行时',
             turn_bounded and outcome['error_type']=='CancelledError' and outcome['fatal'] is False,
             dict(outcome,joined=turn_bounded))
        step('B4 运行时与 reader 都已结束',
             state['alive'] is False and after['reader_alive'] is False
             and after['exit']['alive'] is False,
             {'alive':state['alive'],'reader_alive':after['reader_alive'],
              'exit_alive':after['exit']['alive'],'readers':live_readers()})
        step('B5 Reasonix 主进程与派生子进程都不存在',
             process_gone and not alive_children,
             {'pid':pid,'pid_exists_after':psutil.pid_exists(pid),
              'children_before':children_before,'children_after':children_after,
              'living_children':alive_children})
        step('B6 无 pending RPC / 无 pending interaction / 不再 ready',
             after['pending_requests']==[] and state['ready'] is False
             and adapter.request_methods()[-1:]==['session/close'],
             {'pending_requests_after':after['pending_requests'],'ready':state['ready'],
              'last_method':adapter.request_methods()[-1:],
              'pending_rpc_before_stop':pending_rpc})
        step('B7 全程无协议错误',not after['runtime']['protocol'],
             {'protocol_errors':after['runtime']['protocol']})
        report['probe_b']={'pid':pid,'session_id':session_id,'turn':outcome,
                           'stop_seconds':outcome_holder.get('seconds'),
                           'pending_rpc_before_stop':pending_rpc,
                           'children_before':children_before,'children_after':children_after,
                           'process_gone':process_gone}
    except Exception as error:
        report.setdefault('errors',{})['probe_b']={'type':type(error).__name__,
                                                   'message':str(error)[:500]}
    finally:
        Runtime.send=_original_send
        if adapter is not None:
            try:adapter.stop()
            except Exception:pass


try:
    probe_a()
    probe_b()
finally:
    Runtime.send=_original_send

report['wire']=list(outbound)
report['ok']=bool(report['steps']) and all(item['ok'] for item in report['steps']) \
    and not report.get('errors')
print(json.dumps(report,ensure_ascii=False,indent=2))
if temporary and not args.keep:shutil.rmtree(storage,ignore_errors=True)
raise SystemExit(0 if report['ok'] else 1)
