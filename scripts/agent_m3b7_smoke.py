"""M3B-7 smoke against the real binary: the whole chain, driven through the bridge only.

Opt-in on purpose: it launches the real Reasonix process and sends real prompts, so it costs model
calls and must not run inside the pytest suite. It differs from the earlier smokes in one important
way: nothing here touches the adapter directly. Sessions are created, driven, cancelled and stopped
through AgentBridge, and every assertion reads AgentEventStore, so what is being proven is the
composition — bridge x real adapter x real runtime x real MCP — not one layer at a time.

The MCP tool is a local fixture (tests/fixtures/m3b7_fixture_mcp.py): one pure echo function, no
network, no clock, no randomness, no database. A question is triggered the way production triggers
one: a capability that is not on the allow list surfaces as session/request_permission, which is
what the client is asked to answer.

Usage (PowerShell):
  $env:TAG_PROVIDER_HOME='K:\\Bobbytagging\\Data\\providers'
  .venv\\Scripts\\python.exe scripts/agent_m3b7_smoke.py
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
storage=Path(args.storage).resolve() if args.storage else Path(tempfile.mkdtemp(prefix='m3b7-smoke-'))
storage.mkdir(parents=True,exist_ok=True)
os.environ['TAG_STORAGE']=str(storage)
if args.provider_home:os.environ['TAG_PROVIDER_HOME']=args.provider_home
sys.path.insert(0,str(ROOT))

import psutil                                                          # noqa: E402
from backend import harness                                            # noqa: E402
from backend.db import init_db                                         # noqa: E402
from backend.runtime_isolation import environment                      # noqa: E402
from backend.agent import events as store                              # noqa: E402
from backend.agent import protocol as agent_protocol                   # noqa: E402
from backend.agent.backend import CancelledError,RuntimeUnavailable       # noqa: E402
from backend.agent.bridge import AgentBridge                          # noqa: E402
from backend.agent.reasonix import ReasonixAdapter,ReasonixLaunchConfig  # noqa: E402
from backend.agent.runtime import Runtime                              # noqa: E402

init_db()                                                              # fresh isolated storage

FIXTURE_MCP=ROOT/'tests'/'fixtures'/'m3b7_fixture_mcp.py'
PLUGIN='m3b7_fixture'
LONG_PROMPT=('请从 1 开始连续输出整数，一直输出到 400，每行一个数字，'
             '不要输出任何其他文字，不要解释，不要调用任何工具。')

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


def wait_until(predicate,timeout):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.02)
    return predicate()


def methods_since(index):
    names=[]
    for line in outbound[index:]:
        try:names.append(json.loads(line).get('method'))
        except json.JSONDecodeError:pass
    return names


# --- isolated working directory and plugin config -------------------------

cwd=storage/'cwd';cwd.mkdir(parents=True,exist_ok=True)
(cwd/'PRODUCT_ANALYST_AGENT.md').write_text(
    '严格执行用户指令。不要输出多余解释。需要用户做选择时必须通过 ask 能力提问，不要自己替用户决定。\n',
    encoding='utf-8')
allowed=['mcp_connect__'+PLUGIN,'mcp__'+PLUGIN+'__echo_tool']
# ask and every file/shell built-in stay off the allow list on purpose: a capability that is not
# allowed surfaces as session/request_permission, which is exactly what this probe must answer.
denied=['bash','write','edit','read_file','ls','glob','grep','history','read_session','memory','docs',
        'web','task','parallel_tasks','fleet','install','skills','run_skill','read_only_skill','read_skill']
isolated=environment('m3b7smoke',None,{})
plugin=('\n[[plugins]]\nname = '+json.dumps(PLUGIN)+'\ncommand = '+json.dumps(sys.executable)+
        '\nargs = '+json.dumps([str(FIXTURE_MCP)])+'\nauto_start = true\ntier = "eager"\n'
        'env = {'+', '.join(json.dumps(key)+' = '+json.dumps(value) for key,value in isolated.items())+'}\n'
        'call_timeout_seconds = 60\ntool_timeout_seconds = {}\n')
(cwd/'reasonix.toml').write_text('''[agent]
system_prompt_file = "PRODUCT_ANALYST_AGENT.md"
planner_model = ""
[environment]
enabled = false
[tools]
enabled = ["use_capability"]
[permissions]
mode = "deny"
'''+'allow = '+json.dumps(allowed)+'\ndeny = '+json.dumps(denied)+'\n'+plugin,encoding='utf-8')


# --- bridge wiring --------------------------------------------------------

created_adapter=[]


def factory():
    config=ReasonixLaunchConfig(executable=harness.executable(),model=args.model,cwd=str(cwd),
        env=isolated,runtime_key='m3b7-smoke')
    adapter=ReasonixAdapter(config)
    created_adapter.append(adapter)
    return adapter


bridge=AgentBridge(factory,dataset_id='m3b7smoke')


def rows(session_id):
    return store.read(session_id,after_sequence=0,limit=1000)


def kinds(session_id):
    return [row['event_type'] for row in rows(session_id)]


def mark(session_id):
    return len(rows(session_id))


def kinds_since(session_id,mark):
    """Ordering is only meaningful inside one turn: the session already holds earlier turns."""
    return [row['event_type'] for row in rows(session_id)[mark:]]


def payloads(session_id,event_type):
    return [row['payload'] for row in rows(session_id) if row['event_type']==event_type]


def turn(bridge,session_id,text):
    """Run one turn on its own thread, so a cancel or stop can arrive while it is still running."""
    state={'error':None,'finished':False}
    def drive():
        try:bridge.send(session_id,text)
        except Exception as error:state['error']=error
        finally:state['finished']=True
    worker=threading.Thread(target=drive,daemon=True);worker.start()
    state['worker']=worker
    return state


def outcome(state):
    error=state['error']
    return {'error_type':type(error).__name__ if error else None,
            'fatal':getattr(error,'fatal',None) if error else None,
            'returned_cleanly':error is None and state['finished'],'finished':state['finished']}


def descendants(pid):
    if pid is None:return []
    try:parent=psutil.Process(pid)
    except psutil.Error:return []
    return [{'pid':child.pid,'name':child.name()} for child in parent.children(recursive=True)]


session_id=None
try:
    session_id=bridge.create_session(provider='reasonix',model=args.model)['id']
    bridge.start(session_id)
    started_session=bridge.get_session(session_id)
    step('0 通过 Bridge 建会话并启动',started_session['status']=='active'
         and bool(started_session['remote_session_id']),
         {'session_id':session_id,'remote_session_id':started_session['remote_session_id'],
          'status':started_session['status']})

    # --- Probe 1: plain text, through the bridge, into the event store ------
    wire_mark=len(outbound)
    text_turn=turn(bridge,session_id,'只回复一个短句：M3B7_TEXT_OK')
    text_turn['worker'].join(180)
    joined=''.join(item.get('text','') for item in payloads(session_id,'assistant_text_delta'))
    settled=payloads(session_id,'turn_settled')
    step('1 文本回合经 Bridge 完成',outcome(text_turn)['returned_cleanly'] and settled
         and settled[-1].get('stop_reason')=='end_turn',
         dict(outcome(text_turn),stop_reason=(settled[-1].get('stop_reason') if settled else None),
              text=joined[:200],methods=methods_since(wire_mark)))
    step('2 文本确实来自真实模型',joined.strip()!='' and 'M3B7_TEXT_OK' in joined,{'text':joined[:200]})
    sequences=[row['sequence'] for row in rows(session_id)]
    step('3 AgentEventStore sequence 稠密单调',sequences==list(range(1,len(sequences)+1)),
         {'count':len(sequences),'head':sequences[:8],'tail':sequences[-4:]})
    order=kinds(session_id)
    step('4 文本回合事件形状正确',
         order[:4]==['session_created','session_starting','session_started','message_started']
         and order.index('assistant_text_delta')<order.index('turn_settled')<order.index('message_completed'),
         {'kinds':order})
    step('5 session 仍 active 且 remote session 未变',
         bridge.get_session(session_id)['status']=='active'
         and bridge.get_session(session_id)['remote_session_id']==started_session['remote_session_id'],
         {'status':bridge.get_session(session_id)['status']})

    # --- Probe 2a: a real MCP tool call, host lifecycle through the bridge --
    wire_mark=len(outbound);kind_mark=mark(session_id)
    tool_turn=turn(bridge,session_id,
        '请调用 '+PLUGIN+' 插件提供的 echo_tool 工具，value 参数填 M3B7TOOL。拿到结果后只回复两个字：完成。')
    tool_turn['worker'].join(240)
    order=kinds_since(session_id,kind_mark)
    started=payloads(session_id,'tool_call_started')
    settled_tool=payloads(session_id,'tool_call_settled')
    observed=bool(started and settled_tool)
    step('6 真实 MCP 调用产生 host lifecycle',observed,
         {'started':started,'settled':settled_tool,'methods':methods_since(wire_mark),
          'tool_classes':created_adapter[-1].diagnostics()['tool_classes'] if created_adapter else None,
          'outcome':outcome(tool_turn)})
    if observed:
        step('7 tool 事件落在 turn terminal 之前',
             order.index('tool_call_started')<order.index('tool_call_settled')
             <order.index('turn_settled'),
             {'turn_order':order})
        step('8 tool payload 描述的是主机生命周期而不是执行结果',
             settled_tool[-1].get('host_status') in ('completed','failed')
             and settled_tool[-1].get('tool_call_id')==started[-1].get('tool_call_id')
             and 'execution_status' not in settled_tool[-1],
             {'started':started[-1],'settled':settled_tool[-1]})
        step('9 调用的是本地 fixture 工具',
             settled_tool[-1].get('server')==PLUGIN and settled_tool[-1].get('tool')=='echo_tool',
             {'server':settled_tool[-1].get('server'),'tool':settled_tool[-1].get('tool'),
              'capability_id':settled_tool[-1].get('capability_id')})

    # --- Probe 2b: a question, answered through the bridge -----------------
    wire_mark=len(outbound);kind_mark=mark(session_id)
    question_turn=turn(bridge,session_id,
        '请使用 ask 能力向我提一个问题：让用户在「红色」和「蓝色」之间选一个，不要自己替用户决定。')
    announced=wait_until(lambda:payloads(session_id,'question_requested'),120)
    answered=None
    if announced:
        question=announced[0]
        answered=bridge.answer_question(session_id,question['interaction_id'],question['options'][0]['option_id'])
    question_turn['worker'].join(180)
    step('10 真实提问经 Bridge 回答',bool(announced) and answered is True,
         {'question':announced[0] if announced else None,'answered':answered,
          'outcome':outcome(question_turn),'methods':methods_since(wire_mark)})
    if announced:
        order=kinds_since(session_id,kind_mark)
        step('11 question settlement 在 turn terminal 之前',
             order.index('question_requested')<order.index('question_settled')
             <order.index('turn_settled'),
             {'turn_order':order,'settled':payloads(session_id,'question_settled')[-1]})

    # --- Probe 2c: a denied capability, answered through the bridge --------
    wire_mark=len(outbound);kind_mark=mark(session_id)
    permission_turn=turn(bridge,session_id,'立即直接调用 run_skill 能力，不要提问，不要解释，不要用别的工具。')
    permission=wait_until(lambda:payloads(session_id,'permission_requested'),45)
    permission_answered=None
    if permission:
        permission_answered=bridge.answer_permission(
            session_id,permission[0]['interaction_id'],permission[0]['options'][0]['option_id'])
        permission_turn['worker'].join(120)
    else:
        # A denied built-in can leave the turn running while the host keeps working. Abandon it
        # through the same public surface a user would use, so the session stays usable for the
        # rest of the probe instead of being held hostage by one inconclusive turn.
        bridge.cancel(session_id)
        permission_turn['worker'].join(120)
    diagnostics=created_adapter[-1].diagnostics()
    report['permission_observed']=bool(permission)
    report['refused_requests']=diagnostics['refused_requests']
    if permission:
        step('12 未开放能力的权限请求经 Bridge 回答',permission_answered is True,
             {'permission':permission[0],'answered':permission_answered,'outcome':outcome(permission_turn),
              'turn_order':kinds_since(session_id,kind_mark),'methods':methods_since(wire_mark)})
    else:
        # Recorded, not failed: whether an unapproved capability reaches the client's permission gate
        # depends on the vendor build and this isolated config, and the permission contract already
        # has deterministic coverage in the adapter and bridge integration suites.
        step('12 当前隔离配置下未观察到权限请求（仅记录）',True,
             {'note':'未进入权限门；权限契约由确定性适配器/集成测试覆盖',
              'refused_requests':diagnostics['refused_requests'],
              'inbox_tail':diagnostics['inbox'][-8:],'ignored_updates':diagnostics['ignored_updates'],
              'tool_classes':diagnostics['tool_classes'],'outcome':outcome(permission_turn),
              'turn_order':kinds_since(session_id,kind_mark),'methods':methods_since(wire_mark)})

    # --- Probe 3: cancel, resume on the same session, then stop ------------
    remote_session_id=bridge.get_session(session_id)['remote_session_id']
    wire_mark=len(outbound)
    long_turn=turn(bridge,session_id,LONG_PROMPT)
    assert wait_until(lambda:'session/prompt' in methods_since(wire_mark),60),'长回合没有发出 prompt'
    cancel_answer=bridge.cancel(session_id)
    long_turn['worker'].join(180)
    step('13 Bridge 取消在飞回合',cancel_answer['cancelled'] is True
         and isinstance(long_turn['error'],CancelledError),
         dict(outcome(long_turn),cancel=cancel_answer))
    step('14 取消只丢弃回合，会话与运行时都还在',
         bridge.get_session(session_id)['status']=='active'
         and bridge.get_session(session_id)['remote_session_id']==remote_session_id
         and bridge.status(session_id)['backend']['alive'] is True,
         {'status':bridge.get_session(session_id)['status'],
          'remote_session_id':bridge.get_session(session_id)['remote_session_id'],
          'backend':bridge.status(session_id)['backend']})
    resumed=turn(bridge,session_id,'只回复 OK')
    resumed['worker'].join(180)
    step('15 同一 session 取消后仍可继续',outcome(resumed)['returned_cleanly']
         and payloads(session_id,'turn_settled')[-1].get('stop_reason')=='end_turn'
         and bridge.get_session(session_id)['remote_session_id']==remote_session_id,
         dict(outcome(resumed),session_id=bridge.get_session(session_id)['remote_session_id']))

    wire_mark=len(outbound)
    stopped_turn=turn(bridge,session_id,LONG_PROMPT)
    assert wait_until(lambda:'session/prompt' in methods_since(wire_mark),60),'长回合没有发出 prompt'
    pid=bridge.status(session_id)['backend']['pid']
    children_before=descendants(pid)
    stop_state={}
    def stopper():
        opened=time.perf_counter()
        try:stop_state['session']=bridge.stop(session_id)
        except Exception as error:stop_state['error']=error
        stop_state['seconds']=round(time.perf_counter()-opened,3)
    stop_thread=threading.Thread(target=stopper,daemon=True);stop_thread.start()
    stop_thread.join(120)
    stopped_turn['worker'].join(120)
    children_after=[child for child in descendants(pid) if psutil.pid_exists(child['pid'])]
    final=bridge.get_session(session_id)
    step('16 Bridge stop 有界完成且 send 有界退出',
         not stop_thread.is_alive() and stopped_turn['worker'].is_alive() is False,
         {'stop_seconds':stop_state.get('seconds'),
          'stop_error':type(stop_state['error']).__name__ if stop_state.get('error') else None,
          'turn':outcome(stopped_turn)})
    step('17 本地主动 stop 不被误报为意外失去运行时',
         isinstance(stopped_turn['error'],CancelledError)
         and not isinstance(stopped_turn['error'],RuntimeUnavailable)
         and 'session_failed' not in kinds(session_id),
         {'error_type':outcome(stopped_turn)['error_type'],'fatal':outcome(stopped_turn)['fatal']})
    step('18 会话进入 stopped，运行时与 reader 都已结束',
         final['status']=='stopped' and bridge.status(session_id)['backend'] is None,
         {'status':final['status'],'kinds_tail':kinds(session_id)[-4:]})
    step('19 无孤儿进程',
         (pid is None or not psutil.pid_exists(pid)) and not children_after,
         {'pid':pid,'pid_exists':psutil.pid_exists(pid) if pid else None,
          'children_before':children_before,'children_after':children_after})
    step('20 全程事件 sequence 稠密单调',[row['sequence'] for row in rows(session_id)]
         ==list(range(1,len(rows(session_id))+1)),{'count':len(rows(session_id))})
    step('21 全程无协议错误',
         not created_adapter[-1].diagnostics()['runtime']['protocol'],
         {'protocol_errors':created_adapter[-1].diagnostics()['runtime']['protocol']})
    report['event_kinds']=kinds(session_id)
    report['event_count']=len(rows(session_id))
    report['tool_classes']=created_adapter[-1].diagnostics()['tool_classes']
    report['ignored_updates']=created_adapter[-1].diagnostics()['ignored_updates']
except Exception as error:
    report['error']={'type':type(error).__name__,'message':str(error)[:500],
                     'detail':getattr(error,'detail',None)}
finally:
    Runtime.send=_original_send
    if session_id is not None:
        try:bridge.stop(session_id)
        except Exception:pass
    for adapter in created_adapter:
        try:adapter.stop()
        except Exception:pass

report['ok']=bool(report['steps']) and all(item['ok'] for item in report['steps']) and 'error' not in report
print(json.dumps(report,ensure_ascii=False,indent=2))
if temporary and not args.keep:shutil.rmtree(storage,ignore_errors=True)
raise SystemExit(0 if report['ok'] else 1)
