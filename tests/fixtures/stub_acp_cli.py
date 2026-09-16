"""Deterministic ACP-shaped stdio peer for runtime tests. It is not a model: it only replies
with fixed frames so the transport can be tested offline and without spending quota.

Modes (argv[1]): normal | hang | silent | exit | noise | crash_after_prompt
                 initialize_fail | session_new_fail | close_exit_1 | protocol_version_mismatch
                 no_resume_capability | no_reload_capability
                 resume_fail | resume_invalid | resume_no_echo
                 reload_fail | reload_invalid
                 prompt_fail | prompt_multi | prompt_extra
An optional argv[2] names a file this peer appends every received frame to, so a test can assert
the exact request shape instead of inferring it.

A caller that has to occupy argv itself (an adapter that owns its own launch command) sets the
same two values through STUB_MODE and STUB_LOG instead.
"""
import json
import os
import subprocess
import sys
import time

KNOWN_MODES={'normal','hang','silent','exit','noise','crash_after_prompt','noisy_stderr',
             'initialize_fail','session_new_fail','close_exit_1','protocol_version_mismatch',
             'no_resume_capability','no_reload_capability','resume_fail','resume_invalid',
             'resume_no_echo','reload_fail','reload_invalid',
             'prompt_fail','prompt_multi','prompt_extra','prompt_slow','tool_failed_context_cancelled',
             'tool_dup_start','tool_dup_settle','tool_flip_status','tool_orphan_update',
             'tool_non_pending_start','tool_missing_status','tool_unknown_status','tool_multi',
             'tool_internal','tool_discovery','tool_handler_failed',
             'tool_missing_status_then_terminal','tool_unknown_start_then_terminal',
             'prompt_cancelled','prompt_cancelled_with_tool','prompt_unknown_stop',
             'prompt_missing_stop','prompt_null_stop','prompt_numeric_stop','prompt_cancelled_once',
             'ask_client','ask_client_cancelled','permission_client','malformed_client','ask_client_once',
             'cancel_aware','cancel_aware_no_stop','cancel_aware_tool','cancel_aware_once',
             'ignore_cancel','hang_once','unsolicited_update','prompt_fail_once','spawn_child'}
_MODE_ARG=sys.argv[1] if len(sys.argv)>1 else None
MODE=_MODE_ARG if _MODE_ARG in KNOWN_MODES else (os.environ.get('STUB_MODE') or 'normal')
LOG=os.environ.get('STUB_LOG')
if LOG is None and _MODE_ARG in KNOWN_MODES and len(sys.argv)>2:LOG=sys.argv[2]
RELOAD_METHOD='_reasonix.io/session/reloadExtensions'
# A turn's terminal response. Anything not listed gets the ordinary end_turn.
STOP_RESPONSES={
    'prompt_cancelled':{'stopReason':'cancelled'},
    'prompt_cancelled_with_tool':{'stopReason':'cancelled'},
    'prompt_unknown_stop':{'stopReason':'future_reason'},
    'prompt_missing_stop':{},
    'prompt_null_stop':{'stopReason':None},
    'prompt_numeric_stop':{'stopReason':7},
    'prompt_cancelled_once':{'stopReason':'cancelled'},
}
PROMPT_COUNT={'n':0}
ASKED={'n':0}
CANCELLED={'n':0}
# A Python peer on Windows defaults to the console codepage; the transport expects UTF-8. stdin
# matters as much as stdout: a non-ASCII prompt decoded with the wrong code page yields lone
# surrogates that cannot be written back out.
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
sys.stderr.write('stub ready\n')
sys.stderr.flush()


def emit(message):
    sys.stdout.write(json.dumps(message,ensure_ascii=False)+'\n')
    sys.stdout.flush()


def raw(text):
    sys.stdout.write(text+'\n')
    sys.stdout.flush()


def record(message):
    if not LOG:return
    with open(LOG,'a',encoding='utf-8') as handle:
        handle.write(json.dumps(message,ensure_ascii=False)+'\n')


def updates():
    emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_thought_chunk','content':{'text':'thinking'}}}})
    emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'text':'你好'}}}})
    emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{
        'sessionUpdate':'tool_call','toolCallId':'c1','title':'use_capability','status':'pending',
        'rawInput':{'action':'call','capability_id':'mcp-tool:product_workbench/read_worksheet'}}}})
    emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{
        'sessionUpdate':'tool_call_update','toolCallId':'c1','status':'completed'}}})


def question_request_frame(request_id=900):
    """A real question-shaped permission request, trimmed from the v1.25.1 wire."""
    return {'jsonrpc':'2.0','id':request_id,'method':'session/request_permission','params':{
        'sessionId':'stub-session',
        'toolCall':{'toolCallId':'ask-1-q1','title':'Which format should I use?','kind':'other',
                    'status':'pending',
                    'content':[{'type':'content','content':{'type':'text','text':'Intent'}}],
                    'rawInput':{'id':'q1','multi':False,
                                'options':[{'Label':'表格','Description':'用表格呈现'},
                                           {'Label':'列表','Description':'用列表呈现'}],
                                'question':'Which format should I use?'}},
        'options':[{'optionId':'q1:1','name':'表格 - 用表格呈现','kind':'allow_once'},
                   {'optionId':'q1:2','name':'列表 - 用列表呈现','kind':'allow_once'},
                   {'optionId':'q1:cancel','name':'Cancel','kind':'reject_once'}]}}


def permission_request_frame(request_id=901):
    """A permission request that is not a question. The real tool-level shape is unverified, so
    only the generic fields the wire actually carries are used."""
    return {'jsonrpc':'2.0','id':request_id,'method':'session/request_permission','params':{
        'sessionId':'stub-session',
        'toolCall':{'toolCallId':'call_perm_1','title':'run_skill','kind':'other','status':'pending'},
        'options':[{'optionId':'perm:1','name':'Allow once','kind':'allow_once'},
                   {'optionId':'perm:2','name':'Reject','kind':'reject_once'}]}}


def malformed_request_frame(request_id=902):
    """A permission request with nothing answerable in it."""
    return {'jsonrpc':'2.0','id':request_id,'method':'session/request_permission',
            'params':{'sessionId':'stub-session'}}


def await_inbound_reply():
    """Block until the client answers the pending inbound request, then return its frame."""
    for line in sys.stdin:
        line=line.strip()
        if not line:continue
        try:message=json.loads(line)
        except json.JSONDecodeError:continue
        record(message)
        if 'method' in message:continue
        return message
    return None


def chunk(text):
    return {'jsonrpc':'2.0','method':'session/update','params':{
        'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}}


def tool_call_frame(call_id,capability='mcp-tool:probe/slow_probe',status='pending',
                    action='call',title='use_capability',arguments=None,raw=None):
    """A tool_call frame shaped like the real wire. status=None omits the field entirely."""
    update={'sessionUpdate':'tool_call','toolCallId':call_id,'title':title,'kind':'other'}
    if status is not None:update['status']=status
    if raw is not None:update['rawInput']=raw
    elif title=='use_capability':
        payload={'action':action}
        if capability is not None:payload['capability_id']=capability
        if arguments is not None:payload['arguments']=arguments
        update['rawInput']=payload
    return {'jsonrpc':'2.0','method':'session/update','params':{'update':update}}


def tool_update_frame(call_id,status,text=None):
    update={'sessionUpdate':'tool_call_update','toolCallId':call_id,'status':status}
    if text is not None:
        update['content']=[{'type':'content','content':{'type':'text','text':text}}]
    return {'jsonrpc':'2.0','method':'session/update','params':{'update':update}}


def await_cancel(ident,stop='cancelled',announce=True):
    """Stay in the turn until the client cancels it. Returns False when the turn was torn down
    instead, so the caller can stop pretending to answer."""
    if announce:emit(chunk('半'))
    for line in sys.stdin:
        line=line.strip()
        if not line:continue
        try:message=json.loads(line)
        except json.JSONDecodeError:continue
        record(message)
        method=message.get('method')
        if method=='session/cancel':
            if stop=='none':emit({'jsonrpc':'2.0','id':ident,'result':{}})
            else:emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':stop}})
            return True
        if method=='session/close':
            emit({'jsonrpc':'2.0','id':message['id'],'result':{}})
            sys.exit(1)
    return False


def await_close_only():
    """Ignore a cancel and answer only a close: the peer that will not abandon its turn."""
    emit(chunk('半'))
    for line in sys.stdin:
        line=line.strip()
        if not line:continue
        try:message=json.loads(line)
        except json.JSONDecodeError:continue
        record(message)
        if message.get('method')=='session/close':
            emit({'jsonrpc':'2.0','id':message['id'],'result':{}})
            sys.exit(1)


def prompt_stream():
    """What one turn emits before its response. Default keeps the original four-update stream."""
    if MODE=='tool_failed_context_cancelled':
        # Replays only the frame a cancelled turn leaves behind: status failed, "context canceled".
        # It is NOT a full session/cancel fixture — the turn here still ends with end_turn, because
        # the cancel lifecycle belongs to a later step. Its one purpose is to show that a host
        # failure is not upgraded into a claim about the tool's physical execution.
        emit(tool_call_frame('call_cancel_1',arguments={'seconds':10,'label':'cancelled'}))
        emit(tool_update_frame('call_cancel_1','failed','context canceled'))
        emit(chunk('after-cancel'))
        return
    if MODE=='tool_handler_failed':
        # The other real failed shape: the handler itself raised, so the host reports a tool error.
        emit(tool_call_frame('call_raised',arguments={'label':'B'}))
        emit(tool_update_frame('call_raised','failed',
             'plugin tool reported error: Error executing tool slow_probe: probe handler failure for B'))
        return
    if MODE=='prompt_cancelled_with_tool':
        # The host gave up on the turn while the tool itself was reported failed: two independent
        # facts that must both survive.
        emit(chunk('half'))
        emit(tool_call_frame('call_c',arguments={'n':1}))
        emit(tool_update_frame('call_c','failed','context canceled'))
        return
    if MODE=='tool_missing_status_then_terminal':
        # A start that never opened an outward lifecycle must not be closed by a later terminal.
        emit(tool_call_frame('call_void',status=None))
        emit(tool_update_frame('call_void','completed','no start was ever reported'))
        return
    if MODE=='tool_unknown_start_then_terminal':
        emit(tool_call_frame('call_running',status='running'))
        emit(tool_update_frame('call_running','failed','no start was ever reported'))
        return
    if MODE=='tool_dup_start':
        emit(tool_call_frame('call_dup',arguments={'n':1}))
        emit(tool_call_frame('call_dup',arguments={'n':1}))
        emit(tool_update_frame('call_dup','completed','once'))
        return
    if MODE=='tool_dup_settle':
        emit(tool_call_frame('call_dup'))
        emit(tool_update_frame('call_dup','completed','first'))
        emit(tool_update_frame('call_dup','completed','second'))
        return
    if MODE=='tool_flip_status':
        emit(tool_call_frame('call_flip'))
        emit(tool_update_frame('call_flip','completed','first'))
        emit(tool_update_frame('call_flip','failed','second'))
        return
    if MODE=='tool_orphan_update':
        emit(tool_update_frame('call_orphan','completed','nobody started me'))
        return
    if MODE=='tool_non_pending_start':
        emit(tool_call_frame('call_late',status='completed'))
        return
    if MODE=='tool_missing_status':
        emit(tool_call_frame('call_void',status=None))
        return
    if MODE=='tool_unknown_status':
        emit(tool_call_frame('call_running'))
        emit(tool_update_frame('call_running','running','still going'))
        return
    if MODE=='tool_multi':
        emit(tool_call_frame('call_A',arguments={'n':'A'}))
        emit(tool_call_frame('call_B',arguments={'n':'B'}))
        emit(tool_update_frame('call_B','completed','B done'))
        emit(tool_update_frame('call_A','failed','A done'))
        emit(chunk('after-tools'))
        return
    if MODE=='tool_internal':
        emit(tool_call_frame('call_goal',title='update_goal',raw={'status':'complete'}))
        emit(tool_update_frame('call_goal','failed','no goal turn'))
        return
    if MODE=='tool_discovery':
        emit(tool_call_frame('call_list',action='list'))
        emit(tool_update_frame('call_list','completed'))
        return
    if MODE=='prompt_multi':
        for text in ('你','好','M3B3','_OK'):emit(chunk(text))
        return
    if MODE=='prompt_extra':
        emit({'jsonrpc':'2.0','method':'session/update','params':{
            'update':{'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'hidden reasoning'}}}})
        emit({'jsonrpc':'2.0','method':'session/update','params':{
            'update':{'sessionUpdate':'available_commands_update','availableCommands':[]}}})
        emit({'jsonrpc':'2.0','method':'_reasonix.io/session/status_update','params':{'state':'running'}})
        emit({'jsonrpc':'2.0','method':'session/update','params':{
            'update':{'sessionUpdate':'tool_call','toolCallId':'c1','title':'use_capability',
                      'status':'pending','rawInput':{'action':'call'}}}})
        emit(chunk('only-text'))
        return
    updates()


def capabilities():
    """The vendor surface a real peer advertises, minus whatever the mode removes."""
    sessions={}
    if MODE!='no_resume_capability':sessions['resume']={}
    sessions['close']={}
    agent={'sessionCapabilities':sessions}
    if MODE!='no_reload_capability':
        agent['_meta']={'reasonix.io':{'sessionReloadExtensions':{'method':RELOAD_METHOD}}}
    return agent


def main():
    if MODE=='exit':sys.exit(3)
    if MODE=='noisy_stderr':
        for index in range(300):
            sys.stderr.write('noise-%d\n'%index)
        sys.stderr.flush()
    for line in sys.stdin:
        line=line.strip()
        if not line:continue
        message=json.loads(line)
        record(message)
        method=message.get('method');ident=message.get('id')
        if method=='initialize':
            if MODE=='initialize_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub initialize failure'}})
            elif MODE=='protocol_version_mismatch':
                emit({'jsonrpc':'2.0','id':ident,'result':{'protocolVersion':2,'agentCapabilities':capabilities()}})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{'protocolVersion':1,'agentCapabilities':capabilities()}})
        elif method=='session/new':
            if MODE=='session_new_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub session failure'}})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{'sessionId':(message.get('params') or {}).get('sessionId') or 'stub-session'}})
                if MODE=='spawn_child':
                    # A descendant created after the owner associated this peer: only inheritance
                    # can cover it, because nothing here is ever identified by name.
                    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(600)'],
                                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    sys.stderr.write('child-pid=%d\n'%child.pid);sys.stderr.flush()
                if MODE=='unsolicited_update':
                    # One update nobody asked for, sent right after the handshake. It exists so a
                    # test can break the client's read path with no request in flight.
                    emit({'jsonrpc':'2.0','method':'session/update','params':{'update':{
                        'sessionUpdate':'agent_thought_chunk','content':{'text':'unsolicited'}}}})
        elif method=='session/resume':
            if MODE=='resume_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub resume failure'}})
            elif MODE=='resume_invalid':
                emit({'jsonrpc':'2.0','id':ident,'result':'not-an-object'})
            else:
                # A real peer returns the session state and does NOT echo the session id.
                state={'configOptions':[],'models':{},'modes':{}}
                if MODE!='resume_no_echo':state['sessionId']=(message.get('params') or {}).get('sessionId')
                emit({'jsonrpc':'2.0','id':ident,'result':state})
        elif method==RELOAD_METHOD:
            if MODE=='reload_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub reload failure'}})
            elif MODE=='reload_invalid':
                emit({'jsonrpc':'2.0','id':ident,'result':'not-an-object'})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{}})
        elif method=='session/prompt':
            if MODE=='hang':continue
            if MODE=='noise':raw('not json at all');raw('null')
            if MODE=='prompt_slow':time.sleep(0.5)
            if MODE=='cancel_aware':
                await_cancel(ident,'cancelled')
                continue
            if MODE=='cancel_aware_no_stop':
                await_cancel(ident,'none')
                continue
            if MODE=='ignore_cancel':
                await_close_only()
                continue
            if MODE=='hang_once':
                PROMPT_COUNT['n']+=1
                if PROMPT_COUNT['n']==1:continue
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                continue
            if MODE=='prompt_fail_once':
                # One failed turn, then a healthy one: proves a non-fatal TurnError leaves the
                # session usable without needing two sessions.
                PROMPT_COUNT['n']+=1
                if PROMPT_COUNT['n']==1:
                    emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub prompt failure'}})
                    continue
            if MODE=='cancel_aware_tool':
                # A cancelled turn whose tool call the host reported failed: two separate facts.
                emit(chunk('半'))
                emit(tool_call_frame('call_cancel_2',arguments={'n':1}))
                emit(tool_update_frame('call_cancel_2','failed','context canceled'))
                await_cancel(ident,'cancelled',announce=False)
                continue
            if MODE=='cancel_aware_once':
                # Abandons only the first turn, so a later turn can prove the session still works.
                CANCELLED['n']+=1
                if CANCELLED['n']==1:
                    await_cancel(ident,'cancelled')
                    continue
            if MODE=='ask_client_once':
                # Asks on the first turn only, so a later turn can prove the session still works.
                ASKED['n']+=1
                if ASKED['n']==1:
                    emit(question_request_frame())
                    await_inbound_reply()
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                continue
            if MODE=='ask_client':
                # Ask the client something and wait for its answer before the turn can end.
                emit(question_request_frame())
                reply=await_inbound_reply()
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                continue
            if MODE=='ask_client_cancelled':
                emit(question_request_frame())
                await_inbound_reply()
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'error'}})
                continue
            if MODE=='permission_client':
                emit(permission_request_frame())
                await_inbound_reply()
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                continue
            if MODE=='malformed_client':
                emit(malformed_request_frame())
                await_inbound_reply()
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                continue
            prompt_stream()
            if MODE=='crash_after_prompt':sys.exit(4)
            if MODE=='prompt_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub prompt failure'}})
            else:
                PROMPT_COUNT['n']+=1
                if MODE=='prompt_cancelled_once' and PROMPT_COUNT['n']>1:
                    emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
                else:
                    emit({'jsonrpc':'2.0','id':ident,'result':STOP_RESPONSES.get(MODE,{'stopReason':'end_turn'})})
        elif method=='session/close':
            emit({'jsonrpc':'2.0','id':ident,'result':{}})
            sys.exit(1 if MODE=='close_exit_1' else 0)
        elif method=='boom':
            emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub failure'}})
        elif MODE=='silent':
            continue
        else:
            emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'method not found'}})


main()
