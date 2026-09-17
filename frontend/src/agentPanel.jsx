// The agent session panel: HTTP commands out, application events in through the event stream.
//
// It knows sessions, turns, tools, questions and application errors — and nothing about which agent
// runtime answers, how it is addressed or how it is spoken to. The reply text never arrives as a
// command response: it is accumulated from the stream, so there is exactly one event channel.
//
// All decisions about what a status means live in agentState.js; this file wires inputs to it and
// renders the result.
import React,{useEffect,useReducer,useRef,useState}from'react';
import{Send,Pause,Power,RotateCcw}from'lucide-react';
import{agentReducer,initialAgentState,canSend,pendingInteractions,SESSION_STATUS,TURN_STATUS}from'./agentState';
import{activityView,activityVisible}from'./agentActivity';

const LAST_SESSION='agent:last-session';
// A presentation preference, not a data model: it only decides whether a finished card starts open.
const DETAIL_PREFERENCE='agent:activity-detail';
// The event name the stream uses for every frame. It is named, not the SSE default, and a mismatch
// is silent in a browser, so a test cross-checks it against the server constant.
const STREAM_EVENT='agent_event';

// One place that turns an application failure into what the UI shows: a code, a safe message and
// whether the session is finished. A runtime's own text is never read, let alone displayed.
async function call(path,body,method='POST'){
  const response=await fetch(path,body===undefined?{method}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(response.ok)return response.json();
  let detail;
  try{detail=(await response.json()).detail}catch{detail=null}
  if(detail&&typeof detail==='object')
    return Promise.reject({code:detail.code||'error',message:detail.message||'请求失败',fatal:detail.fatal===true});
  return Promise.reject({code:'invalid_request',message:typeof detail==='string'?detail:'请求失败',fatal:false});
}

// One turn's execution, as one card. Reading it top to bottom: what the agent is doing now, how many
// operations are done, and a way into the full timeline. A running card never expands — the point of
// the card is that a long turn cannot push the reply off the screen — and a finished one is a single
// quiet line until the user asks for more.
function TurnActivityCard({item,detail}){
  const [toggled,setToggled]=useState(false);
  const view=activityView(item);
  if(!view||!activityVisible(item))return null;
  const expanded=!view.running&&(toggled||detail===true);
  const glyph=view.running?'◌':(view.status==='done'?'✓':(view.status==='cancelled'?'■':'⚠'));
  const region='activity-timeline-'+item.id;
  return <section className={'turn-activity '+view.status}
    data-activity-status={view.status} data-activity-expanded={expanded?'true':'false'}
    data-tool-count={view.toolCount} data-completed={view.completedCount}
    data-retry={view.retryCount} data-current-action={item.currentTool||''}>
    <div className="activity-line">
      <span className="activity-glyph" aria-hidden="true">{glyph}</span>
      <span className="activity-headline">{view.headline}</span>
      {view.running&&<span className="activity-counts">{view.counts}</span>}
    </div>
    {view.running&&view.detail&&<div className="activity-detail" role="status">{view.detail}</div>}
    <button type="button" className="activity-toggle" aria-expanded={expanded} aria-controls={region}
      onClick={()=>setToggled(open=>!open)}>{view.expandLabel}</button>
    {expanded&&<ul className="activity-timeline" id={region}>
      {view.lines.map(line=><li key={line.id} className={line.status}>
        <span className="activity-glyph" aria-hidden="true">{line.glyph}</span>
        <span className="activity-action">{line.label}</span>
        {line.note&&<span className="activity-note">{line.note}</span>}
        <span className="activity-meta">{line.tool}{line.duration?' · '+line.duration:''}{line.error?' · '+line.error:''}</span>
      </li>)}
    </ul>}
  </section>;
}

function InteractionCard({item,busy,onAnswer,onSkip}){
  const pending=item.status==='pending';
  return <section className={'question-card '+item.status}>
    <div className="question-kicker">{item.kind==='question'?'需要你选择':'需要你授权'}</div>
    <h4>{item.question||item.kind==='permission'?'是否允许这次调用？':'请选择'}</h4>
    {pending&&<div className="question-options">
      {item.options.map(option=><button key={option.option_id} disabled={busy}
        onClick={()=>onAnswer(item,option.option_id)}>
        <b>{option.label||option.name||option.option_id}</b>
        {option.description&&<span>{option.description}</span>}
      </button>)}
    </div>}
    {pending&&<div className="button-row">
      <button disabled={busy} onClick={()=>onSkip(item)}>{item.kind==='question'?'跳过这个问题':'拒绝'}</button>
    </div>}
    {!pending&&<p>{item.status==='answered'?'已提交你的选择。':'已停止本次交互，未执行任何操作。'}</p>}
  </section>;
}

export function AgentPanel({did}){
  const[state,dispatch]=useReducer(agentReducer,initialAgentState);
  const[draft,setDraft]=useState('');
  const[busy,setBusy]=useState('');
  // Off by default: the card is meant to be quiet, and the detail is one click away. It stays a
  // preference — the card never turns back into a message per tool event.
  const[detail,setDetail]=useState(()=>{try{return localStorage.getItem(DETAIL_PREFERENCE)==='1'}catch{return false}});
  const setDetailPreference=value=>{setDetail(value);try{localStorage.setItem(DETAIL_PREFERENCE,value?'1':'0')}catch{}};
  const stream=useRef(null),end=useRef(null);
  const canWrite=canSend(state)&&!!did;
  const waiting=pendingInteractions(state);
  const items=state.items.length;

  useEffect(()=>{end.current?.scrollIntoView({block:'end'})},[items]);

  // One stream per session. The browser resumes it by itself with Last-Event-ID, so a reconnect
  // replays from where the reducer stopped instead of the panel building its own replay.
  useEffect(()=>{
    if(state.sessionStatus!==SESSION_STATUS.ACTIVE||!state.sessionId)return undefined;
    const events=new EventSource(`/api/agent/sessions/${state.sessionId}/events`);
    const receive=message=>{try{dispatch({type:'agent_event',event:JSON.parse(message.data)})}catch{}}
    // The stream names its frames, and a named SSE event is never delivered to onmessage: it has to
    // be listened for by name. Both are registered so an unnamed frame would still be handled, and
    // the reducer's sequence check makes a duplicate delivery harmless either way.
    events.addEventListener(STREAM_EVENT,receive);
    events.onmessage=receive;
    stream.current=events;
    return()=>{events.close();stream.current=null};
  },[state.sessionStatus,state.sessionId]);

  // A remembered session may belong to a process that is gone. Its row still says active, but with
  // no backend nothing can be sent, so the panel must not present it as a live conversation.
  useEffect(()=>{
    const remembered=localStorage.getItem(LAST_SESSION);
    if(!remembered)return;
    call(`/api/agent/sessions/${remembered}`,undefined,'GET')
      .then(view=>dispatch({type:'session_snapshot',snapshot:view}))
      .catch(()=>{localStorage.removeItem(LAST_SESSION);dispatch({type:'reset'})});
  },[]);

  const fail=(error,scope)=>dispatch({type:'command_failed',error,scope});

  const start=async()=>{
    dispatch({type:'session_creating'});
    try{
      const session=await call('/api/agent/sessions',{dataset_id:did});
      dispatch({type:'session_created',sessionId:session.id});
      const started=await call(`/api/agent/sessions/${session.id}/start`);
      dispatch({type:'session_started',sessionId:session.id,remoteSessionId:started.remote_session_id});
      localStorage.setItem(LAST_SESSION,session.id);
    }catch(error){dispatch({type:'reset'});fail(error,'session')}
  };

  const send=async()=>{
    const text=draft.trim();
    if(!text||!canWrite)return;
    dispatch({type:'turn_sending',text});setDraft('');setBusy('send');
    // The response reports the turn's outcome; the reply itself arrives on the stream.
    try{await call(`/api/agent/sessions/${state.sessionId}/messages`,{text});dispatch({type:'turn_accepted'})}
    catch(error){fail(error,'turn')}
    finally{setBusy('')}
  };

  const cancel=async()=>{
    setBusy('cancel');
    try{
      const answer=await call(`/api/agent/sessions/${state.sessionId}/cancel`);
      // Accepted means asked for. The turn stays open until its terminal event arrives.
      dispatch({type:'cancel_requested',accepted:answer.cancel_requested===true});
    }catch(error){fail(error,'turn')}
    finally{setBusy('')}
  };

  const stop=async()=>{
    dispatch({type:'stop_requested'});setBusy('stop');
    try{await call(`/api/agent/sessions/${state.sessionId}/stop`);dispatch({type:'stop_settled'})}
    catch(error){fail(error,'session')}
    finally{setBusy('')}
  };

  const settle=async(item,optionId)=>{
    const kind=item.kind==='question'?'questions':'permissions';
    setBusy(item.interactionId);
    try{
      const answer=await call(`/api/agent/sessions/${state.sessionId}/${kind}/${item.interactionId}/answer`,{option_id:optionId});
      dispatch({type:'interaction_answered',interactionId:item.interactionId,outcome:answer.accepted?'selected':'cancelled'});
    }catch(error){fail(error,'turn')}
    finally{setBusy('')}
  };

  const skip=async item=>{
    const kind=item.kind==='question'?'questions':'permissions';
    setBusy(item.interactionId);
    try{
      await call(`/api/agent/sessions/${state.sessionId}/${kind}/${item.interactionId}/cancel`);
      dispatch({type:'interaction_answered',interactionId:item.interactionId,outcome:'cancelled'});
    }catch(error){fail(error,'turn')}
    finally{setBusy('')}
  };

  const newSession=()=>{localStorage.removeItem(LAST_SESSION);dispatch({type:'reset'})};
  const active=state.sessionStatus===SESSION_STATUS.ACTIVE;
  const busyTurn=state.turnStatus!==TURN_STATUS.IDLE;

  return <div className="agent-panel">
    <div className="agent-status" data-status={state.sessionStatus}>
      <span>会话：{state.sessionStatus}</span>
      <span>本轮：{state.turnStatus}</span>
      <label className="activity-preference" title="默认展开每一轮的执行过程">
        <input type="checkbox" checked={detail} onChange={event=>setDetailPreference(event.target.checked)}/>
        显示详细执行过程
      </label>
      {active&&<button title="结束会话" disabled={busy==='stop'} onClick={stop}><Power size={14}/>结束会话</button>}
      {!active&&state.sessionStatus!==SESSION_STATUS.CREATING&&state.sessionStatus!==SESSION_STATUS.STARTING
        &&<button title="新建会话" disabled={!did} onClick={start}>
          {state.sessionStatus===SESSION_STATUS.UNAVAILABLE?<RotateCcw size={14}/>:null}
          {state.sessionStatus===SESSION_STATUS.UNAVAILABLE?'此会话不可用，新建会话':'启动 Agent'}
        </button>}
    </div>

    {state.error&&<div role="alert" className="alert">{state.error.message}
      {state.error.fatal?'　会话已结束，请新建会话。':'　可以继续发送。'}</div>}

    <div className="chat-scroll">
      {state.items.map(item=>item.kind==='activity'?<TurnActivityCard key={item.id} item={item} detail={detail}/>
        :<div className={'message '+item.kind} key={item.id}>
          <div className="message-name">{item.kind==='user'?'你':'Agent'}{item.status==='streaming'?'正在回复':''}</div>
          <div className="message-text">{item.text}</div>
        </div>)}
      {!state.items.length&&<div className="welcome"><small>NEW AGENT SESSION</small><h2>让 Agent 接管这一轮。</h2>
        <p>启动后会建立一个新的 Agent 会话；回复、工具调用和提问都会实时出现在这里。</p></div>}
      <div ref={end}/>
    </div>

    {waiting.map(item=><InteractionCard key={item.interactionId} item={item} busy={busy===item.interactionId}
      onAnswer={settle} onSkip={skip}/>)}

    <div className="composer">
      <textarea aria-label="Agent 输入" placeholder={active?'描述目标…':'先启动 Agent 会话'}
        value={draft} disabled={!canWrite} onChange={event=>setDraft(event.target.value)}
        onKeyDown={event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.nativeEvent.isComposing){event.preventDefault();send()}}}/>
      <div>
        {busyTurn?<button disabled={state.turnStatus===TURN_STATUS.CANCELLING||busy==='cancel'} onClick={cancel}>
            <Pause size={15}/>{state.turnStatus===TURN_STATUS.CANCELLING?'正在取消':'停止生成'}</button>
          :<button className="primary" disabled={!canWrite||!draft.trim()} onClick={send}><Send size={16}/>发送</button>}
        <span>{state.sessionStatus===SESSION_STATUS.UNAVAILABLE
          ?'此会话已失效，请新建会话':'命令走 HTTP，过程走事件流'}</span>
      </div>
    </div>
  </div>;
}
