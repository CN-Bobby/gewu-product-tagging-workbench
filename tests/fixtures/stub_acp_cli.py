"""Deterministic ACP-shaped stdio peer for runtime tests. It is not a model: it only replies
with fixed frames so the transport can be tested offline and without spending quota.

Modes (argv[1]): normal | hang | silent | exit | noise | crash_after_prompt
                 initialize_fail | session_new_fail | close_exit_1 | protocol_version_mismatch
An optional argv[2] names a file this peer appends every received frame to, so a test can assert
the exact request shape instead of inferring it.

A caller that has to occupy argv itself (an adapter that owns its own launch command) sets the
same two values through STUB_MODE and STUB_LOG instead.
"""
import json
import os
import sys

KNOWN_MODES={'normal','hang','silent','exit','noise','crash_after_prompt','noisy_stderr',
             'initialize_fail','session_new_fail','close_exit_1','protocol_version_mismatch'}
_MODE_ARG=sys.argv[1] if len(sys.argv)>1 else None
MODE=_MODE_ARG if _MODE_ARG in KNOWN_MODES else (os.environ.get('STUB_MODE') or 'normal')
LOG=os.environ.get('STUB_LOG')
if LOG is None and _MODE_ARG in KNOWN_MODES and len(sys.argv)>2:LOG=sys.argv[2]
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
                emit({'jsonrpc':'2.0','id':ident,'result':{'protocolVersion':2,'capabilities':{}}})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{'protocolVersion':1,'capabilities':{}}})
        elif method in ('session/new','session/resume'):
            if MODE=='session_new_fail' and method=='session/new':
                emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub session failure'}})
            else:
                emit({'jsonrpc':'2.0','id':ident,'result':{'sessionId':(message.get('params') or {}).get('sessionId') or 'stub-session'}})
        elif method=='session/prompt':
            if MODE=='hang':continue
            if MODE=='noise':raw('not json at all');raw('null')
            updates()
            if MODE=='crash_after_prompt':sys.exit(4)
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
