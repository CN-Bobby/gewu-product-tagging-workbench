"""Reasonix tool classification: pure wire parsing, no process and no I/O.

The shapes below are trimmed copies of frames captured from the real v1.25.1 wire, so the
classifier is pinned to what the peer actually sends rather than to a guess. Nothing here asserts
anything about what a tool physically did: only the host status is observable.
"""
import pytest

from backend.agent import protocol

DISCOVERY={'sessionUpdate':'tool_call','toolCallId':'call_00_9DX1mH7dgrOiapt88sfL1891',
           'title':'use_capability','kind':'other','status':'pending','rawInput':{'action':'list'}}
MCP_CALL={'sessionUpdate':'tool_call','toolCallId':'call_00_zqpztiee6TSnONd6syk77118',
          'title':'use_capability','kind':'other','status':'pending',
          'rawInput':{'action':'call','capability_id':'mcp-tool:probe/slow_probe',
                      'arguments':{'seconds':3,'label':'m3b4'}}}
INTERNAL={'sessionUpdate':'tool_call','toolCallId':'call_00_ET_asTcphZ2kDmqgJlnYaeq8759',
          'title':'update_goal','kind':'other','status':'pending',
          'rawInput':{'status':'complete','completion':{'verified':[],'unverified':[]}}}
COMPLETED={'sessionUpdate':'tool_call_update','toolCallId':'call_00_zqpztiee6TSnONd6syk77118',
           'status':'completed',
           'content':[{'type':'content','content':{'type':'text','text':'probe complete label=m3b4'}}]}
HANDLER_FAILED={'sessionUpdate':'tool_call_update','toolCallId':'call_00_8lDbuUMfmwjKxU2GLQ1i9343',
                'status':'failed','content':[{'type':'content','content':{'type':'text',
                    'text':'plugin tool reported error: Error executing tool failing_probe: probe handler failure for B'}}]}
CANCEL_FAILED={'sessionUpdate':'tool_call_update','toolCallId':'call_00_mM8yGqIvUB43iATdxNtd4688',
               'status':'failed','content':[{'type':'content','content':{'type':'text','text':'context canceled'}}]}


def merged(*frames):
    """A call state assembled the way the adapter does: keyed by call id, in arrival order."""
    state={}
    for frame in frames:state=protocol.merge_tool_state(state,frame)
    return state


# --- the seven cases taken from the real wire ------------------------------

def test_case_1_capability_discovery_is_not_a_tool_invocation():
    assert protocol.classify_tool_call(DISCOVERY)==protocol.CLASS_CAPABILITY_DISCOVERY
    assert protocol.capability_action(DISCOVERY['rawInput'])=='list'


def test_case_2_a_real_mcp_call_is_classified_and_named():
    assert protocol.classify_tool_call(MCP_CALL)==protocol.CLASS_MCP_TOOL_CALL
    assert protocol.capability_target(MCP_CALL['rawInput'])==('mcp-tool','probe','slow_probe')
    assert protocol.mcp_tool_target(MCP_CALL['rawInput'])==('probe','slow_probe')
    assert protocol.tool_arguments(MCP_CALL['rawInput'])=={'seconds':3,'label':'m3b4'}


def test_case_3_a_non_capability_tool_is_not_a_tool_invocation():
    """update_goal and ask are the internal tools observed on the wire; the class itself only
    claims "this is not a capability proxy call", not that every such call is internal."""
    assert protocol.classify_tool_call(INTERNAL)==protocol.CLASS_NON_CAPABILITY_TOOL
    assert protocol.mcp_tool_target(INTERNAL['rawInput']) is None
    assert protocol.classify_tool_call({'title':'ask'})==protocol.CLASS_NON_CAPABILITY_TOOL


def test_case_4_a_call_to_a_non_mcp_capability_is_other():
    for capability in ('skill:explore','mcp-server:probe','mcp-tool:probe','probe/slow_probe'):
        update={'title':'use_capability','rawInput':{'action':'call','capability_id':capability}}
        assert protocol.classify_tool_call(update)==protocol.CLASS_OTHER_CAPABILITY,capability
    assert protocol.classify_tool_call({'title':'use_capability','rawInput':{'action':'call'}})==protocol.CLASS_OTHER_CAPABILITY
    assert protocol.classify_tool_call({'title':'use_capability','rawInput':{}})==protocol.CLASS_OTHER_CAPABILITY


def test_case_5_a_completed_update_carries_host_status_and_normalized_content():
    state=merged(MCP_CALL,COMPLETED)
    assert protocol.tool_status(COMPLETED)=='completed'
    assert protocol.classify_tool_call(state)==protocol.CLASS_MCP_TOOL_CALL
    assert [block['text'] for block in protocol.normalize_tool_content(COMPLETED)]==['probe complete label=m3b4']


def test_case_6_a_failed_status_stays_only_a_host_status():
    """A wire status must never be turned into a claim about the tool itself."""
    state=merged(MCP_CALL,HANDLER_FAILED)
    assert protocol.tool_status(HANDLER_FAILED)=='failed'
    assert protocol.classify_tool_call(state)==protocol.CLASS_MCP_TOOL_CALL
    assert not hasattr(protocol,'execution_status')
    assert not hasattr(protocol,'tool_execution_status')
    assert not hasattr(protocol,'actual_tool_status')


def test_case_7_two_calls_stay_separate_by_tool_call_id():
    first={'sessionUpdate':'tool_call','toolCallId':'call_A','title':'use_capability',
           'rawInput':{'action':'call','capability_id':'mcp-tool:probe/echo_probe','arguments':{'value':'one'}}}
    second={'sessionUpdate':'tool_call','toolCallId':'call_B','title':'use_capability',
            'rawInput':{'action':'call','capability_id':'mcp-tool:probe/echo_probe','arguments':{'value':'two'}}}
    states={}
    for frame in (first,{'sessionUpdate':'tool_call_update','toolCallId':'call_A','status':'completed'},
                  second,{'sessionUpdate':'tool_call_update','toolCallId':'call_B','status':'failed'}):
        call_id=protocol.tool_call_id(frame)
        states[call_id]=protocol.merge_tool_state(states.get(call_id,{}),frame)
    assert sorted(states)==['call_A','call_B']
    assert states['call_A']['status']=='completed' and states['call_B']['status']=='failed'
    assert protocol.tool_arguments(states['call_A']['rawInput'])=={'value':'one'}
    assert protocol.tool_arguments(states['call_B']['rawInput'])=={'value':'two'}
    assert len(states)==2


# --- the cancel ambiguity guard -------------------------------------------

def test_a_host_failed_frame_is_not_upgraded_to_a_tool_failure():
    """CANCEL_FAILED only replays the frame a cancelled turn leaves behind: status failed with
    "context canceled". The full cancel lifecycle (stopReason=cancelled) belongs to a later step,
    so this fixture proves one thing only — the protocol layer never turns a host failure into a
    claim about the tool's physical execution. The text is fixture content, never a decision
    input."""
    state=merged(MCP_CALL,CANCEL_FAILED)
    assert protocol.tool_status(CANCEL_FAILED)=='failed'
    assert protocol.classify_tool_call(state)==protocol.CLASS_MCP_TOOL_CALL
    assert [block['text'] for block in protocol.normalize_tool_content(CANCEL_FAILED)]==['context canceled']


def test_classification_ignores_the_status_entirely():
    pending=protocol.classify_tool_call(MCP_CALL)
    for status in ('pending','completed','failed','cancelled',None):
        update=dict(MCP_CALL,status=status)
        assert protocol.classify_tool_call(update)==pending,status


def test_is_capability_proxy_is_not_the_real_invocation_test():
    """is_capability_proxy answers 'is this the proxy', which is true for a directory listing and a
    real invocation alike, so it can never stand in for the real-invocation test."""
    assert protocol.is_capability_proxy(DISCOVERY)
    assert protocol.is_capability_proxy(MCP_CALL)
    assert protocol.classify_tool_call(DISCOVERY)!=protocol.CLASS_MCP_TOOL_CALL
    assert protocol.classify_tool_call(MCP_CALL)==protocol.CLASS_MCP_TOOL_CALL


# --- arguments and content -------------------------------------------------

def test_arguments_are_returned_exactly_as_sent():
    arguments=protocol.tool_arguments(MCP_CALL['rawInput'])
    assert arguments=={'seconds':3,'label':'m3b4'}
    assert arguments is MCP_CALL['rawInput']['arguments']
    nested={'rawInput':{'arguments':{'深的':'值','list':[1,2,{'a':None}]}}}
    assert protocol.tool_arguments(nested['rawInput'])=={'深的':'值','list':[1,2,{'a':None}]}
    assert protocol.tool_arguments({'arguments':None}) is None
    assert protocol.tool_arguments({'arguments':'{"seconds":3}'}) is None


def test_content_normalization_lifts_known_text_and_preserves_unknown_blocks():
    update={'content':[{'type':'content','content':{'type':'text','text':'hello'}},
                       {'type':'image','data':'AAAA'},
                       {'type':'content','content':{'type':'resource','uri':'x'}}]}
    blocks=protocol.normalize_tool_content(update)
    assert len(blocks)==3
    assert blocks[0]=={'raw':update['content'][0],'text':'hello'}
    assert blocks[1]['text'] is None and blocks[1]['raw']==update['content'][1]
    assert blocks[2]['text'] is None and blocks[2]['raw']==update['content'][2]
    assert protocol.normalize_tool_content({})==[]
    assert protocol.normalize_tool_content({'content':None})==[]
    assert protocol.normalize_tool_content({'content':'odd'})==[{'raw':'odd','text':None}]


# --- malformed input -------------------------------------------------------

HOSTILE=[None,{},[],0,'text',{'rawInput':None},{'rawInput':'x'},{'rawInput':[]},{'rawInput':{}},
         {'rawInput':{'action':None}},{'rawInput':{'action':123}},{'rawInput':{'action':''}},
         {'rawInput':{'capability_id':None}},{'rawInput':{'capability_id':123}},
         {'rawInput':{'capability_id':'mcp-tool:'}},{'rawInput':{'capability_id':'mcp-tool:probe'}},
         {'rawInput':{'capability_id':'mcp-tool:/slow_probe'}},{'rawInput':{'capability_id':'mcp-tool:probe/'}},
         {'rawInput':{'action':'call','capability_id':'mcp-tool:probe/slow_probe','arguments':'nope'}},
         {'rawInput':{'action':'call','capability_id':'mcp-tool:probe/slow_probe','arguments':[1]}},
         {'title':None},{'title':123},{'title':'','rawInput':{}},{'title':'use_capability'}]


@pytest.mark.parametrize('value',HOSTILE)
def test_tool_helpers_degrade_safely_on_malformed_input(value):
    action=protocol.capability_action(value)
    assert action is None or isinstance(action,str)
    target=protocol.capability_target(value)
    assert target is None or (isinstance(target,tuple) and len(target)==3)
    mcp=protocol.mcp_tool_target(value)
    assert mcp is None or (isinstance(mcp,tuple) and len(mcp)==2)
    arguments=protocol.tool_arguments(value)
    assert arguments is None or isinstance(arguments,dict)
    assert isinstance(protocol.normalize_tool_content(value),list)
    status=protocol.tool_status(value)
    assert status is None or isinstance(status,str)
    assert protocol.classify_tool_call(value) in (protocol.CLASS_CAPABILITY_DISCOVERY,
        protocol.CLASS_MCP_TOOL_CALL,protocol.CLASS_NON_CAPABILITY_TOOL,
        protocol.CLASS_OTHER_CAPABILITY,protocol.CLASS_UNKNOWN)


def test_a_malformed_mcp_identifier_is_never_a_real_invocation():
    for bad in ('mcp-tool:','mcp-tool:probe','mcp-tool:/slow_probe','mcp-tool:probe/',':x/y','mcp-tool'):
        update={'title':'use_capability','rawInput':{'action':'call','capability_id':bad}}
        assert protocol.classify_tool_call(update)==protocol.CLASS_OTHER_CAPABILITY,bad
        assert protocol.mcp_tool_target({'capability_id':bad}) is None,bad
    assert protocol.capability_target({'capability_id':'mcp-tool:probe/slow_probe'})==('mcp-tool','probe','slow_probe')


def test_a_frame_without_a_title_is_unknown_rather_than_non_capability():
    assert protocol.classify_tool_call({'rawInput':{'action':'call'}})==protocol.CLASS_UNKNOWN
    assert protocol.classify_tool_call({'title':'','rawInput':{}})==protocol.CLASS_UNKNOWN
