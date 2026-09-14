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

def tool_status(update):
    """The Reasonix host status on the wire: pending / completed / failed.

    Host observable state only. ``failed`` can mean the host stopped waiting while the real tool
    kept running, so it is never evidence about what the tool actually did.
    """
    value=update.get('status') if isinstance(update,dict) else None
    return value if isinstance(value,str) and value else None

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


# --- Reasonix capability / tool classification ----------------------------
#
# One tool frame is NOT one tool execution: a single turn can carry several, and only a capability
# call whose target is an MCP tool corresponds to a workbench tool invocation at all. The status on
# the wire is the host's disposition for that call, never evidence about what the underlying tool
# did — a cancelled turn reports ``failed`` while the tool keeps running. These helpers therefore
# parse and classify only; nothing here infers an execution outcome.

CAPABILITY_PROXY='use_capability'
CAPABILITY_CALL='call'
DISCOVERY_ACTIONS=('list','inspect','decline')
MCP_TOOL_SCHEME='mcp-tool'

CLASS_CAPABILITY_DISCOVERY='capability_discovery'
CLASS_MCP_TOOL_CALL='mcp_tool_call'
# Anything that is not the capability proxy. The members observed so far (update_goal, ask) are
# Reasonix-internal tools, but the wire only proves "this is not the capability proxy", so the
# class claims exactly that and nothing stronger.
CLASS_NON_CAPABILITY_TOOL='non_capability_tool'
CLASS_OTHER_CAPABILITY='other_capability'
CLASS_UNKNOWN='unknown'


def capability_action(raw_input):
    """The proxy action (``list``/``inspect``/``decline``/``call``), or None when unusable."""
    value=raw_input.get('action') if isinstance(raw_input,dict) else None
    return value if isinstance(value,str) and value else None


def capability_target(raw_input):
    """Parse ``<scheme>:<server>/<tool>`` into a ``(scheme,server,tool)`` tuple.

    Anything that does not parse returns None: a malformed identifier must degrade to
    "unidentified" instead of raising or being mistaken for a real MCP tool.
    """
    value=raw_input.get('capability_id') if isinstance(raw_input,dict) else None
    if not isinstance(value,str):return None
    scheme,colon,remainder=value.partition(':')
    if not colon or not scheme or not remainder:return None
    server,slash,tool=remainder.partition('/')
    if not slash or not server or not tool:return None
    return (scheme,server,tool)


def mcp_tool_target(raw_input):
    """The ``(server,tool)`` of a real MCP tool invocation, or None for everything else."""
    target=capability_target(raw_input)
    return None if target is None or target[0]!=MCP_TOOL_SCHEME else (target[1],target[2])


def tool_arguments(raw_input):
    """The invocation arguments exactly as sent, or None when absent or not an object.

    Nothing is serialized, rewritten, dropped or redacted here; whether any of it may be persisted
    is a separate concern.
    """
    value=raw_input.get('arguments') if isinstance(raw_input,dict) else None
    return value if isinstance(value,dict) else None


def classify_tool_call(update):
    """Which kind of tool activity one frame describes.

    Pass a ``tool_call`` frame, or a state merged across a call and its updates, so the title and
    rawInput survive. The status is deliberately not consulted: a failed call is the same kind of
    call as a successful one.
    """
    if not isinstance(update,dict):return CLASS_UNKNOWN
    title=update.get('title')
    if not isinstance(title,str) or not title:return CLASS_UNKNOWN
    if title!=CAPABILITY_PROXY:return CLASS_NON_CAPABILITY_TOOL
    raw_input=update.get('rawInput')
    action=capability_action(raw_input)
    if action in DISCOVERY_ACTIONS:return CLASS_CAPABILITY_DISCOVERY
    if action==CAPABILITY_CALL and mcp_tool_target(raw_input) is not None:return CLASS_MCP_TOOL_CALL
    return CLASS_OTHER_CAPABILITY


def normalize_tool_content(update):
    """Content blocks of a tool update: known text lifted, unknown blocks preserved verbatim.

    Deliberately not ``tool_result_text``: the wire is not guaranteed to carry text, so a
    structured or future block shape survives untouched instead of being flattened away.
    """
    raw=update.get('content') if isinstance(update,dict) else None
    items=raw if isinstance(raw,list) else ([] if raw is None else [raw])
    blocks=[]
    for item in items:
        inner=item.get('content') if isinstance(item,dict) else None
        text=inner.get('text') if isinstance(inner,dict) and inner.get('type')=='text' else None
        blocks.append({'raw':item,'text':text if isinstance(text,str) else None})
    return blocks


# --- Reasonix permission / question classification -------------------------
#
# The peer asks for tool permission and for user input through one method, so the two have to be
# told apart structurally rather than by an id prefix or by reading the text. Nothing here says
# what a tool did or will do: a request is only ever a request.

def permission_is_question(params):
    """A question is a request whose rawInput carries a non-empty question text."""
    raw=permission_raw_input(params)
    return bool(raw) and bool(str(raw.get('question') or '').strip())

def permission_is_multi(params):
    raw=permission_raw_input(params)
    return isinstance(raw,dict) and is_multi_question(raw)

def permission_question_text(params):
    raw=permission_raw_input(params) or {}
    return str(raw.get('question') or '').strip()

def permission_question_id(params):
    raw=permission_raw_input(params) or {}
    value=raw.get('id')
    return value.strip() if isinstance(value,str) and value.strip() else None

def permission_wire_options(params):
    """The answerable options exactly as offered: only their wire ids may be answered."""
    options=[]
    for option in (params.get('options') or []):
        if not isinstance(option,dict):continue
        option_id=wire_option_id(option)
        if not option_id:continue
        name=option.get('name') or option.get('label') or ''
        options.append({'option_id':str(option_id),'name':str(name),
                        'kind':str(option.get('kind') or '')})
    return options

def question_options(params):
    """Wire options paired with their display text, positionally.

    The answerable ids live in params.options while the readable labels live in
    toolCall.rawInput.options; only the wire id can be answered, so a label is decoration and is
    never a response value. Options without a display counterpart keep their own name.
    """
    display=[]
    raw=permission_raw_input(params) or {}
    for option in (raw.get('options') or []):
        label,description=option_text(option)
        display.append((label or '',description or ''))
    options=[]
    for index,wire in enumerate(permission_wire_options(params)):
        label,description=display[index] if index<len(display) else ('','')
        options.append({'option_id':wire['option_id'],'label':label or wire['name'],
                        'description':description})
    return options
