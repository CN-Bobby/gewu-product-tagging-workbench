"""Deterministic ACP-shaped stdio peer for runtime tests. It is not a model: it only replies
with fixed frames so the transport can be tested offline and without spending quota.

Modes (argv[1]): normal | hang | silent | exit | noise | crash_after_prompt
"""
import json
import sys

MODE=sys.argv[1] if len(sys.argv)>1 else 'normal'
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
    for line in sys.stdin:
        line=line.strip()
        if not line:continue
        message=json.loads(line)
        method=message.get('method');ident=message.get('id')
        if method=='initialize':
            emit({'jsonrpc':'2.0','id':ident,'result':{'protocolVersion':1,'capabilities':{}}})
        elif method in ('session/new','session/resume'):
            emit({'jsonrpc':'2.0','id':ident,'result':{'sessionId':(message.get('params') or {}).get('sessionId') or 'stub-session'}})
        elif method=='session/prompt':
            if MODE=='hang':continue
            if MODE=='noise':raw('not json at all');raw('null')
            updates()
            if MODE=='crash_after_prompt':sys.exit(4)
            emit({'jsonrpc':'2.0','id':ident,'result':{'stopReason':'end_turn'}})
        elif method=='session/close':
            emit({'jsonrpc':'2.0','id':ident,'result':{}});return
        elif method=='boom':
            emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32000,'message':'stub failure'}})
        elif MODE=='silent':
            continue
        else:
            emit({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'method not found'}})


main()
