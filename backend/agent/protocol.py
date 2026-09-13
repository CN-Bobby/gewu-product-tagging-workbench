"""Reasonix ACP v1 / JSON-RPC wire format.

Pure functions only: no process, no I/O, no database, no business types. Parse failures are
reported as ProtocolError instead of being swallowed, so a caller may log or ignore them.
"""
import json

METHOD_NOT_FOUND=-32601
SESSION_UPDATE='session/update'
PERMISSION='session/request_permission'
CLIENT_UNSUPPORTED='Client method not supported'


class ProtocolError(Exception):
    """A line or message that does not conform to JSON-RPC / ACP."""
    def __init__(self,kind,raw=''):
        self.kind=kind
        self.raw=str(raw)[:500]
        super().__init__(kind)


# --- encode ---------------------------------------------------------------

def request(ident,method,params):
    return {'jsonrpc':'2.0','id':ident,'method':method,'params':params}

def notification(method,params):
    return {'jsonrpc':'2.0','method':method,'params':params}

def response(ident,result):
    return {'jsonrpc':'2.0','id':ident,'result':result}

def error_response(ident,code,message):
    return {'jsonrpc':'2.0','id':ident,'error':{'code':code,'message':message}}

def line(message):
    return json.dumps(message,ensure_ascii=False)+'\n'


# --- decode ---------------------------------------------------------------

def parse_line(text):
    """Return the decoded object or raise ProtocolError. Never returns a non-object."""
    try:message=json.loads(text)
    except json.JSONDecodeError as error:raise ProtocolError('invalid_json',text) from error
    if not isinstance(message,dict):raise ProtocolError('not_an_object',text)
    return message

def message_id(message):return message.get('id')
def method_of(message):return message.get('method')
def params_of(message):return message.get('params') or {}
def result_of(message):return message.get('result',{})
def has_method(message):return 'method' in message
def is_notification(message):return 'method' in message and 'id' not in message
def is_request(message):return 'method' in message and 'id' in message
def is_response(message):return 'method' not in message and 'id' in message

def error_message(message,default='ACP error'):
    return str((message.get('error') or {}).get('message',default))

def is_error(message):return 'error' in message


# --- ACP session/update classification -----------------------------------

def update_of(message):return params_of(message).get('update') or {}
def update_kind(update):return update.get('sessionUpdate')
def chunk_text(update):return (update.get('content') or {}).get('text','')
def tool_call_id(update):return update.get('toolCallId')
def tool_status(update):return update.get('status')

def merge_tool_state(previous,update):
    """tool_call / tool_call_update only carry the fields they change."""
    return {**previous,**{k:update.get(k) for k in ('title','kind','status','rawInput') if update.get(k) is not None}}

def is_capability_proxy(state,server='product_workbench',proxy_title='use_capability'):
    """Reasonix reaches optional tools (MCP servers, skills) through one proxy tool."""
    title=state.get('title')
    return title==proxy_title or server in str(title or '')


# --- ACP permission classification ---------------------------------------

def permission_tool(params):return params.get('toolCall') or {}
def permission_tool_title(params,default='未知工具'):return permission_tool(params).get('title') or default
def allow_once_options(params):
    return [x for x in (params.get('options') or []) if isinstance(x,dict) and x.get('kind')=='allow_once']

def permission_raw_input(params):
    """The raw input arrives as an object or as a JSON string; None when unusable."""
    raw=permission_tool(params).get('rawInput')
    if isinstance(raw,str):
        try:raw=json.loads(raw)
        except json.JSONDecodeError:return None
    return raw if isinstance(raw,dict) else None

def is_multi_question(raw):return raw.get('multi') is True or raw.get('multiSelect') is True
def wire_option_id(option):return option.get('optionId') or option.get('option_id')

def option_text(option):
    """A wire option is either a plain string or a labelled object."""
    if isinstance(option,str):return option,''
    if isinstance(option,dict):
        label=option.get('label') or option.get('Label') or option.get('name') or option.get('title')
        description=option.get('description') or option.get('Description') or option.get('detail') or ''
        return label,description
    return '',''
