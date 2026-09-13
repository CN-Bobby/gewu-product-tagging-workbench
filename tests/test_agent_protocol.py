"""Protocol layer: pure encode/decode of JSON-RPC and ACP frames. No process, no I/O."""
import json
import pytest
from backend.agent import protocol


def test_encode_request_notification_response_and_error():
    assert protocol.request(7,'session/new',{'cwd':'x'})=={'jsonrpc':'2.0','id':7,'method':'session/new','params':{'cwd':'x'}}
    assert protocol.notification('session/cancel',{'sessionId':'s'})=={'jsonrpc':'2.0','method':'session/cancel','params':{'sessionId':'s'}}
    assert protocol.response(7,{'ok':True})=={'jsonrpc':'2.0','id':7,'result':{'ok':True}}
    assert protocol.error_response(7,protocol.METHOD_NOT_FOUND,protocol.CLIENT_UNSUPPORTED)=={'jsonrpc':'2.0','id':7,'error':{'code':-32601,'message':'Client method not supported'}}


def test_line_is_newline_terminated_and_keeps_non_ascii():
    text=protocol.line(protocol.response(1,{'text':'您好'}))
    assert text.endswith('\n') and text.count('\n')==1
    assert json.loads(text)['result']['text']=='您好'


def test_decode_accepts_valid_jsonrpc_frames():
    request=protocol.parse_line('{"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":1}}')
    assert protocol.has_method(request) and protocol.is_request(request) and not protocol.is_notification(request)
    assert protocol.message_id(request)==3 and protocol.method_of(request)=='initialize'
    assert protocol.params_of(request)=={'protocolVersion':1}

    notification=protocol.parse_line('{"jsonrpc":"2.0","method":"session/update","params":{}}')
    assert protocol.has_method(notification) and protocol.is_notification(notification)
    assert protocol.message_id(notification) is None

    response=protocol.parse_line('{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}')
    assert protocol.is_response(response) and not protocol.has_method(response)
    assert protocol.result_of(response)=={'stopReason':'end_turn'} and not protocol.is_error(response)


def test_decode_reports_invalid_json_instead_of_swallowing_it():
    with pytest.raises(protocol.ProtocolError) as error:protocol.parse_line('not json at all')
    assert error.value.kind=='invalid_json' and error.value.raw=='not json at all'


def test_decode_reports_non_object_frames():
    for text in ('null','[1,2]','"text"','123'):
        with pytest.raises(protocol.ProtocolError) as error:protocol.parse_line(text)
        assert error.value.kind=='not_an_object'


def test_remote_error_frames_carry_their_message():
    message=protocol.parse_line('{"jsonrpc":"2.0","id":9,"error":{"code":-32000,"message":"stub failure"}}')
    assert protocol.is_error(message) and protocol.is_response(message)
    assert protocol.error_message(message)=='stub failure'
    assert protocol.error_message(protocol.parse_line('{"id":1,"error":{}}'))=='ACP error'


def test_session_update_classification():
    message=protocol.parse_line(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'text':'你好'}}}}))
    update=protocol.update_of(message)
    assert protocol.update_kind(update)=='agent_message_chunk'
    assert protocol.chunk_text(update)=='你好'
    assert protocol.update_of(protocol.parse_line('{"method":"session/update","params":{}}'))=={}


def test_tool_call_merge_keeps_previous_fields_and_reports_capability_proxy():
    first={'sessionUpdate':'tool_call','toolCallId':'c1','title':'use_capability','status':'pending',
           'rawInput':{'action':'call','capability_id':'mcp-tool:product_workbench/read_worksheet'}}
    second={'sessionUpdate':'tool_call_update','toolCallId':'c1','status':'completed'}
    state=protocol.merge_tool_state({},first)
    assert state['title']=='use_capability' and state['status']=='pending'
    state=protocol.merge_tool_state(state,second)
    assert state['status']=='completed' and state['title']=='use_capability' and state['rawInput']==first['rawInput']
    assert protocol.tool_call_id(first)=='c1' and protocol.tool_status(second)=='completed'
    assert protocol.is_capability_proxy(state)
    assert not protocol.is_capability_proxy({'title':'read_file'})
    assert protocol.merge_tool_state({},first)==protocol.merge_tool_state({},first)


def test_permission_classification_parses_object_and_string_raw_input():
    raw={'id':'color','question':'主色口径？','options':[{'label':'底色'},{'label':'整套'}]}
    params={'toolCall':{'toolCallId':'ask-1','kind':'other','title':'ask','rawInput':json.dumps(raw)},
            'options':[{'optionId':'1','kind':'allow_once'},{'optionId':'2','kind':'allow_once'},{'optionId':'r','kind':'reject_once'}]}
    message=protocol.request(4,protocol.PERMISSION,params)
    assert protocol.permission_tool_title(protocol.params_of(message))=='ask'
    assert protocol.permission_raw_input(protocol.params_of(message))==raw
    assert [x['optionId'] for x in protocol.allow_once_options(protocol.params_of(message))]==['1','2']
    assert not protocol.is_multi_question(raw)
    assert protocol.is_multi_question({**raw,'multiSelect':True})
    assert protocol.permission_raw_input({'toolCall':{'rawInput':'{broken'}}) is None
    assert protocol.permission_raw_input({'toolCall':{'rawInput':'[1]'}}) is None
    assert protocol.permission_tool_title({})=='未知工具'


def test_option_text_handles_strings_objects_and_unknowns():
    assert protocol.option_text('甲')==('甲','')
    assert protocol.option_text({'Label':'乙','Description':'说明'})==('乙','说明')
    assert protocol.option_text({'label':'丙'})==('丙','')
    assert protocol.option_text({'name':'丁'})==('丁','')
    # An object without any known label key yields no label, which callers treat as invalid.
    assert protocol.option_text({'nothing':'x'})==(None,'')
    assert protocol.option_text(None)==('','')
    assert protocol.wire_option_id({'option_id':'1'})=='1'
    assert protocol.wire_option_id({'optionId':'2'})=='2'
    assert protocol.wire_option_id({}) is None
