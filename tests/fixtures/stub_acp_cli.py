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
import sys
import time

KNOWN_MODES={'normal','hang','silent','exit','noise','crash_after_prompt','noisy_stderr',
             'initialize_fail','session_new_fail','close_exit_1','protocol_version_mismatch',
             'no_resume_capability','no_reload_capability','resume_fail','resume_invalid',
             'resume_no_echo','reload_fail','reload_invalid',
             'prompt_fail','prompt_multi','prompt_extra','prompt_slow'}
_MODE_ARG=sys.argv[1] if len(sys.argv)>1 else None
MODE=_MODE_ARG if _MODE_ARG in KNOWN_MODES else (os.environ.get('STUB_MODE') or 'normal')
LOG=os.environ.get('STUB_LOG')
if LOG is None and _MODE_ARG in KNOWN_MODES and len(sys.argv)>2:LOG=sys.argv[2]
RELOAD_METHOD='_reasonix.io/session/reloadExtensions'
# A Python peer on Windows defaults to the console codepage; the transport expects UTF-8.
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


def chunk(text):
    return {'jsonrpc':'2.0','method':'session/update','params':{
        'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}}


def prompt_stream():
    """What one turn emits before its response. Default keeps the original four-update stream."""
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
            prompt_stream()
            if MODE=='crash_after_prompt':sys.exit(4)
            if MODE=='prompt_fail':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub prompt failure'}})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
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
