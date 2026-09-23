// The keyword research page: fill a form, count the budget, confirm, watch the stages, download.
//
// It is a second top-level page, not a dataset view: it owns its own form, its own run projection and
// its own stream, and it never touches the worksheet draft stream or the agent session.
//
// Two channels, one truth each: the snapshot from GET /runs/{id} is authoritative (first paint and
// every reconnect ask for it), and the SSE channel only pushes events. There is no polling loop.
import React,{useEffect,useReducer,useRef,useState}from'react';
import{Download,Play,Plus,RefreshCw,Trash2,X}from'lucide-react';
import './keywordResearch.css';
import{ENDPOINTS,SSE_EVENT_NAME,buildRunRequest,initialKeywordForm,initialKeywordState,
  isActiveStatus,isTerminalEvent,isTerminalStatus,keywordReducer,parseAsins,parseNodeIds}
  from'./keywordState';

// The shared workbench helper turns a failed response into a plain Error, which flattens a
// structured 400 body ({detail:{errors:[...]}}) into "[object Object]". This page has to show every
// problem at once, so the two validating endpoints read the response themselves.
async function callJson(path,body,method='POST'){
  const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  let payload=null;
  try{payload=await response.json()}catch{payload=null}
  if(!response.ok){
    const failure=new Error('请求失败');
    failure.errors=errorList(payload);
    throw failure;
  }
  return payload;
}

function errorList(payload){
  const detail=payload&&payload.detail;
  if(detail&&Array.isArray(detail.errors)&&detail.errors.length)return detail.errors.map(String);
  if(typeof detail==='string'&&detail)return [detail];
  if(detail&&typeof detail.message==='string')return [detail.message];
  return ['请求失败'];
}

const LABEL={
  created:'已创建',queued:'排队中',running:'执行中',cancelling:'正在取消',done:'已完成',
  failed:'失败',cancelled:'已取消',interrupted:'已中断',skipped:'已跳过',idle:'尚未发起',
};
const canQueueStatus=status=>['created','failed','cancelled','interrupted','skipped'].includes(status);

const bytesText=value=>{
  if(value==null||!Number.isFinite(Number(value)))return'—';
  const size=Number(value);
  return size<1024?size+' B':(size/1024).toFixed(1)+' KB';
};

function GroupFields({group,index,meta,onChange,onRemove,canRemove}){
  const parsed=parseAsins(group.asinsText);
  const nodes=parseNodeIds(group.nodeIdsText);
  return <fieldset className="keyword-group">
    <legend>站点组 {index+1}</legend>
    <div className="keyword-group-grid">
      <label className="settings-field"><span className="field-label">站点</span>
        <select aria-label={'站点组 '+(index+1)+' 站点'} value={group.marketplace} onChange={event=>onChange({marketplace:event.target.value})}>
          <option value="">请选择站点</option>
          {(meta?meta.marketplaces:[]).map(name=><option key={name} value={name}>{name}</option>)}
        </select>
        <small className="field-help">站点列表由后端下发，前端不自行维护一份。</small>
      </label>
      <label className="settings-field"><span className="field-label">榜单取数条数</span>
        <select aria-label={'站点组 '+(index+1)+' 榜单取数条数'} value={group.nodeTopN} onChange={event=>onChange({nodeTopN:Number(event.target.value)})}>
          {(meta?meta.node_top_n_options:[]).map(value=><option key={value} value={value}>Top {value}</option>)}
        </select>
        <small className="field-help">仅在有 Node ID 时生效。</small>
      </label>
    </div>
    <label className="settings-field"><span className="field-label">ASIN</span>
      <textarea aria-label={'站点组 '+(index+1)+' ASIN'} value={group.asinsText} placeholder="每行一个，或用逗号、分号、顿号分隔"
        onChange={event=>onChange({asinsText:event.target.value})}/>
      <small className="field-help">
        已识别 {parsed.valid.length} 个（去重后）；单个站点组最多 20 个。
        {parsed.invalid.length>0?' 已忽略：'+parsed.invalid.join('、'):''}
      </small>
    </label>
    <label className="settings-field"><span className="field-label">Node ID</span>
      <textarea aria-label={'站点组 '+(index+1)+' Node ID'} value={group.nodeIdsText} placeholder="可选，每行一个类目节点"
        onChange={event=>onChange({nodeIdsText:event.target.value})}/>
      <small className="field-help">已识别 {nodes.length} 个类目节点；ASIN 与 Node 至少要有一个。</small>
    </label>
    {canRemove&&<div className="button-row"><button type="button" onClick={onRemove}><Trash2 size={13}/>移除这个站点组</button></div>}
  </fieldset>;
}

function BudgetCard({plan}){
  if(!plan)return null;
  const budget=plan.budget||{};
  return <section className="keyword-card">
    <h3>预算核算</h3>
    <p className="keyword-hash">计划指纹 <code>{plan.plan_hash}</code></p>
    <table className="keyword-table">
      <thead><tr><th>站点</th><th>直接 ASIN</th><th>Node</th><th>榜单页</th><th>反查调用</th><th>矩阵</th><th>趋势</th><th>合计</th></tr></thead>
      <tbody>{(budget.groups||[]).map(item=><tr key={item.marketplace}>
        <td>{item.marketplace}</td><td>{item.direct_asins}</td><td>{item.node_count}</td>
        <td>{item.node_pages}</td><td>{item.pool_batch_calls}</td><td>{item.matrix_calls}</td>
        <td>{item.trend_calls}</td><td><b>{item.total}</b></td>
      </tr>)}</tbody>
    </table>
    <p>预计总调用 {budget.planned_total} 次 · 硬上限 {budget.hard_cap} 次 · 基准外重试 {budget.retry_budget} 次</p>
    {(plan.notes||[]).map(note=><small key={note} className="field-help">{note}</small>)}
    <p className="field-help">方案变化后需要重新核算，指纹一致才允许发起。</p>
  </section>;
}

function RunCard({state,form,onStart,onCancel,onDownload,onRefresh,busy}){
  const active=isActiveStatus(state.status);
  const progress=state.progress;
  return <section className="keyword-card">
    <h3>运行进度</h3>
    <div className="keyword-run-head">
        <span className={'keyword-state '+state.status}>{LABEL[state.status]||state.status}</span>
      <span>{state.runId?state.runId.slice(0,12)+'…':'尚未发起'}</span>
      {state.stage&&<span>阶段 {state.stage}</span>}
      {state.plannedCalls!=null&&<span>计划调用 {state.plannedCalls} 次</span>}
    </div>
    {state.message&&<p>{state.message}</p>}
    {progress&&<div className="keyword-progress">
      <div className="keyword-progress-line"><b>{progress.stage}</b>
        <span>{progress.done}{progress.total?' / '+progress.total:''}{progress.unit?' '+progress.unit:''}</span></div>
      {progress.total>0&&<div className="keyword-bar"><i style={{width:Math.min(100,Math.round(progress.done/progress.total*100))+'%'}}/></div>}
      {progress.message&&<small className="field-help">{progress.message}</small>}
    </div>}
    {state.error&&<p className="keyword-error">运行失败：{state.error}</p>}
    <div className="button-row">
        <button className="primary" disabled={!form.planHash||busy==='start'} onClick={onStart}>
        <Play size={14}/>创建任务</button>
      <button disabled={!active||busy==='cancel'||!state.runId} onClick={onCancel}>取消运行</button>
      <button disabled={!state.runId} onClick={onRefresh}><RefreshCw size={13}/>刷新快照</button>
    </div>
    {state.artifacts.length>0&&<div className="keyword-artifacts">
      <h4>产出文件</h4>
      {state.artifacts.map(item=><div className="keyword-artifact" key={item.name}>
        <span>{item.name}</span>
        <small>{bytesText(item.bytes)}{item.sha256?' · SHA256 '+item.sha256.slice(0,12)+'…':''}</small>
        <button className="primary" onClick={()=>onDownload(item.name)}><Download size={14}/>下载</button>
      </div>)}
    </div>}
    <details className="keyword-log" open={active}>
      <summary>过程记录 {state.log.length} 条</summary>
      {state.log.length?state.log.map(item=><p key={item.sequence}><span>{item.sequence}</span>{item.line}</p>)
        :<small className="field-help">还没有事件。</small>}
    </details>
  </section>;
}

export function KeywordResearch({api,selectedRunId='',onSelectRun=()=>{},onSettings=()=>{}}){
  const [meta,setMeta]=useState(null);
  const [credential,setCredential]=useState(null);
  const [secret,setSecret]=useState('');
  const [form,setForm]=useState(initialKeywordForm);
  const [state,dispatch]=useReducer(keywordReducer,initialKeywordState);
  const [plan,setPlan]=useState(null);
  const [errors,setErrors]=useState([]);
  const [recent,setRecent]=useState([]),[queue,setQueue]=useState({items:[],state:'idle',dispatch_enabled:false}),[cursor,setCursor]=useState(''),[nextCursor,setNextCursor]=useState(null),[showHidden,setShowHidden]=useState(false),[view,setView]=useState(selectedRunId?'detail':location.hash.startsWith('#/keyword/queue')?'queue':'new'),[selected,setSelected]=useState([]),[displayName,setDisplayName]=useState(''),[models,setModels]=useState(null),[modelRef,setModelRef]=useState(''),[credentialId,setCredentialId]=useState('');
  const [busy,setBusy]=useState('');
  const stream=useRef(null),latest=useRef(state);
  latest.current=state;

  useEffect(()=>{let cancelled=false;
    api(ENDPOINTS.meta(),undefined,'GET').then(data=>{if(cancelled)return;
      setMeta(data);setCredential(data.credential||null);
      setForm(current=>({...current,
        matrixEnabled:data.matrix_default!==false,
        groups:current.groups.map(group=>({
          ...group,
          marketplace:group.marketplace||(data.marketplaces||[])[0]||'',
          nodeTopN:(data.node_top_n_options||[]).includes(group.nodeTopN)?group.nodeTopN
            :((data.node_top_n_options||[])[0]||50),
        }))}));
    }).catch(error=>{if(!cancelled)setErrors([error.message])});
    api('/api/settings/models',undefined,'GET').then(value=>{if(cancelled)return;setModels(value);const initial=value.defaults?.[0]||value.providers?.flatMap(p=>p.model_refs||[])[0]||'';setModelRef(initial);const provider=(value.providers||[]).find(p=>p.name===initial.split('/')[0]);setCredentialId(provider?.primary_credential_id||'')}).catch(error=>{if(!cancelled)setErrors([error.message])});
    return()=>{cancelled=true};
  },[]);

  const refreshRecent=(next=cursor)=>api(`${ENDPOINTS.runs()}?limit=50${next?'&cursor='+encodeURIComponent(next):''}${showHidden?'&hidden=true':''}`,undefined,'GET')
    .then(data=>{setRecent(data.runs||[]);setNextCursor(data.next_cursor||null)}).catch(()=>{});

  const refreshQueue=()=>api(ENDPOINTS.queue(),undefined,'GET').then(value=>setQueue({...value,items:value.items||value.queue||[]})).catch(error=>setErrors([error.message]));
  useEffect(()=>{refreshRecent('');refreshQueue()},[showHidden]);
  useEffect(()=>{if(selectedRunId){setView('detail');readSnapshot(selectedRunId)}else if(view==='detail')setView('queue')},[selectedRunId]);

  // One stream per run. It is opened from the sequence this page has already folded in, and after a
  // drop the snapshot is read again before resuming: the snapshot can say interrupted after a
  // restart, which no event will ever report.
  useEffect(()=>{
    if(!state.runId||state.finished)return undefined;
    let closed=false,source=null;
    const connect=()=>{
      if(closed)return;
      source=new EventSource(ENDPOINTS.runEvents(state.runId,latest.current.lastSequence));
      // The frames are named, and a named SSE event is never delivered to onmessage.
      source.addEventListener(SSE_EVENT_NAME,message=>{
        let event=null;
        try{event=JSON.parse(message.data)}catch{return}
        dispatch({type:'run_event',event});
        if(isTerminalEvent(event&&event.event_type)){source.close();refreshRecent()}
      });
      source.onerror=()=>{
        if(closed||source===null)return;
        const dropped=source;source=null;dropped.close();
        api(ENDPOINTS.run(state.runId),undefined,'GET').then(snapshot=>{
          if(closed)return;
          dispatch({type:'snapshot',snapshot});
          if(!isTerminalStatus(snapshot.status))connect();
        }).catch(()=>{});
      };
    };
    connect();
    return()=>{closed=true;if(source)source.close()};
  },[state.runId,state.finished]);

  // Any edit invalidates the counted budget, so the confirmation has to be asked for again.
  const edit=mutator=>{
    setErrors([]);
    setPlan(null);
    setForm(current=>({...current,...mutator(current),planHash:''}));
  };
  const editGroup=(key,mutator)=>edit(current=>({
    groups:current.groups.map(group=>group.key===key?{...group,...mutator}:group),
  }));

  const countBudget=async()=>{
    setBusy('plan');
    try{
      const counted=await callJson(ENDPOINTS.plan(),{...buildRunRequest(form),model_ref:modelRef,credential_id:credentialId});
      setPlan(counted);setErrors([]);
      setForm(current=>({...current,planHash:counted.plan_hash||''}));
    }catch(error){setPlan(null);setForm(current=>({...current,planHash:''}));setErrors(error.errors||[error.message])}
    finally{setBusy('')}
  };

  const start=async()=>{
    setBusy('start');
    try{
      if(!plan?.plan_hash||!form.planHash)throw Error('请先核算预算');
      const name=displayName.trim()||`关键词调研 ${new Date().toLocaleString()}`;
      if(!window.confirm(`确认创建任务“${name}”？预算约 ${plan.budget?.planned_total??0} 次调用。此操作只保存任务，不开始 SellerSprite 取数。`))return;
      const chosen=plan.selected||plan;
      const created=await callJson(ENDPOINTS.runs(),{...buildRunRequest(form),dispatch:'create',display_name:name,model_ref:chosen.model_ref||modelRef,credential_id:chosen.credential_id||credentialId});
      dispatch({type:'run_created',runId:created.run_id});setErrors([]);refreshRecent('');refreshQueue();onSelectRun(created.run_id);setView('detail');await readSnapshot(created.run_id);
    }catch(error){setErrors(error.errors||[error.message])}
    finally{setBusy('')}
  };

  const enqueue=async ids=>{if(!ids.length)return;const lookup=new Map([...recent,...queue.items].map(item=>[item.run_id||item.id,item]));const items=ids.map(id=>lookup.get(id)).filter(Boolean);const invalid=items.filter(item=>!canQueueStatus(item.status));if(invalid.length){setErrors(['已完成或已在队列中的任务不能重复排队。']);return}const names=items.map(item=>item.display_name||item.name||item.run_id||item.id);const total=items.reduce((sum,item)=>sum+Number(item.budget?.planned_total??item.planned_total??item.planned_calls??0),0);if(!window.confirm(`确认按当前顺序排队以下 ${ids.length} 项任务？\n${names.map((name,index)=>(index+1)+'. '+name).join('\n')}\n合计预算：${total} 次取数调用。`))return;setBusy('queue');try{await callJson(ENDPOINTS.queue(),{run_ids:ids});setErrors([]);setSelected([]);await Promise.all([refreshQueue(),refreshRecent('')]);if(state.runId&&ids.includes(state.runId))await readSnapshot(state.runId);}catch(error){setErrors(error.errors||[error.message])}finally{setBusy('')}};

  const cancel=async()=>{
    setBusy('cancel');
    try{await api(ENDPOINTS.runCancel(state.runId),{})}
    catch(error){setErrors([error.message])}
    finally{setBusy('')}
  };

  const readSnapshot=async runId=>{
    try{dispatch({type:'snapshot',snapshot:await api(ENDPOINTS.run(runId),undefined,'GET')})}
    catch(error){setErrors([error.message])}
  };

  const modelOptions=(models?.providers||[]).flatMap(provider=>(provider.model_refs||[]).map(ref=>({ref,provider}))),selectedProvider=(models?.providers||[]).find(provider=>provider.name===modelRef.split('/')[0]),credentialOptions=selectedProvider?.credentials?.filter(item=>item.enabled&&item.authorized)||[];
  const selectModel=value=>{setModelRef(value);const provider=(models?.providers||[]).find(item=>item.name===value.split('/')[0]);setCredentialId(provider?.primary_credential_id||'');setPlan(null);setForm(current=>({...current,planHash:''}))};
  const reorder=async(id,offset)=>{const items=(queue.items||[]).filter(item=>['created','queued'].includes(item.status)&&item.run_id!==queue.current&&item.id!==queue.current),index=items.findIndex(item=>(item.run_id||item.id)===id),next=index+offset;if(index<0||next<0||next>=items.length)return;const ids=items.map(item=>item.run_id||item.id);[ids[index],ids[next]]=[ids[next],ids[index]];try{await api(ENDPOINTS.queueOrder(),{run_ids:ids},'PUT');await refreshQueue()}catch(error){setErrors([error.message])}};
  const stopQueue=async()=>{if(!window.confirm('停止当前关键词队列？当前任务将取消，尚未开始的任务标记为跳过并保留输入与缓存。'))return;try{await api(ENDPOINTS.queueStop(),{});await refreshQueue();await refreshRecent('')}catch(error){setErrors([error.message])}};
  const continueQueue=async()=>{try{await api(ENDPOINTS.queueContinue(),{});await Promise.all([refreshQueue(),refreshRecent('')]);if(state.runId)await readSnapshot(state.runId)}catch(error){setErrors([error.message])}};
  const rename=async item=>{const id=item.run_id||item.id,name=window.prompt('任务名称',item.display_name||item.name||'关键词调研');if(!name?.trim())return;try{await api(ENDPOINTS.run(id),{display_name:name.trim()},'PATCH');await refreshRecent('')}catch(error){setErrors([error.message])}};
  const hide=async(item,hidden)=>{const id=item.run_id||item.id;try{await api(hidden?ENDPOINTS.runUnhide(id):ENDPOINTS.runHide(id),{});await Promise.all([refreshRecent(''),refreshQueue()])}catch(error){setErrors([error.message])}};
  const trash=async item=>{const id=item.run_id||item.id;if(isActiveStatus(item.status)||['created','queued'].includes(item.status)&&queue.items.some(q=>(q.run_id||q.id)===id&&q.current))return setErrors(['运行中的任务不能移入回收站']);const contents=item.artifacts?.length?item.artifacts.map(file=>file.name).join('、'):'任务输入、缓存、日志与已生成产物';if(!window.confirm(`确认将“${item.display_name||item.name||id}”及其运行目录移入 Windows 回收站？范围：${contents}。失败时会保留任务。`))return;try{await callJson(ENDPOINTS.run(id),{},'DELETE');await Promise.all([refreshRecent(''),refreshQueue()])}catch(error){setErrors(error.errors||[error.message])}};
  const openDirectory=async item=>{try{await api(ENDPOINTS.runDirectory(item.run_id||item.id),{});}catch(error){setErrors([error.message])}};

  const download=async name=>{
    try{
      const response=await fetch(ENDPOINTS.runArtifact(state.runId,name));
      if(!response.ok)throw Error('文件下载失败');
      const url=URL.createObjectURL(await response.blob());
      const anchor=document.createElement('a');
      anchor.href=url;anchor.download=name;anchor.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){setErrors([error.message])}
  };

  const parsedTotal=form.groups.reduce((sum,group)=>sum+parseAsins(group.asinsText).valid.length,0);
  const nodeTotal=form.groups.reduce((sum,group)=>sum+parseNodeIds(group.nodeIdsText).length,0);
  const activityText=item=>{const payload=item?.payload||{};return [item?.event_type?.replaceAll('_',' '),payload.stage,payload.message||payload.line].filter(Boolean).join(' · ')};

  return <div className="keyword-page">
    <header className="keyword-heading">
      <div>
        <div className="eyebrow">KEYWORD RESEARCH</div>
        <h1>关键词调研<span>创建任务、编排队列、追踪执行过程</span></h1>
      </div>
      <div className="button-row"><span className="keyword-summary">{form.groups.length} 个站点组 · {parsedTotal} 个 ASIN · {nodeTotal} 个 Node</span><button onClick={()=>{refreshRecent('');refreshQueue()}}><RefreshCw size={13}/>刷新</button></div>
    </header>
    <nav className="keyword-tabs"><button className={view==='new'?'active':''} onClick={()=>{setView('new');onSelectRun('')}}>新建任务</button><button className={view==='queue'?'active':''} onClick={()=>{setView('queue');onSelectRun('')}}>任务列表与队列</button>{state.runId&&<button className={view==='detail'?'active':''} onClick={()=>{setView('detail');onSelectRun(state.runId)}}>任务详情</button>}<span>{queue.state||'队列待命'} · {queue.items.length} 项</span></nav>

    {errors.length>0&&<div role="alert" className="keyword-errors">
      <div className="keyword-errors-head"><strong>有 {errors.length} 处需要处理</strong>
        <button onClick={()=>setErrors([])}><X size={15}/></button></div>
      <ul>{errors.map((message,index)=><li key={index}>{message}</li>)}</ul>
    </div>}

    <div className={'keyword-grid keyword-view-'+view}>
      {view==='new'&&<section className="keyword-card">
        <label className="settings-field"><span className="field-label">任务名称</span><input value={displayName} onChange={event=>setDisplayName(event.target.value)} placeholder="例如：核心词调研"/></label>
        <h3>调研输入</h3>
        {form.groups.map((group,index)=><GroupFields key={group.key} group={group} index={index} meta={meta}
          canRemove={form.groups.length>1} onChange={patch=>editGroup(group.key,patch)}
          onRemove={()=>edit(current=>({groups:current.groups.filter(item=>item.key!==group.key)}))}/>)}
        <div className="button-row">
          <button onClick={()=>edit(current=>({groups:[...current.groups,{key:'g'+Date.now(),
            marketplace:(meta&&meta.marketplaces&&meta.marketplaces[0])||'',asinsText:'',nodeIdsText:'',
            nodeTopN:(meta&&meta.node_top_n_options&&meta.node_top_n_options[0])||50}]}))}>
            <Plus size={14}/>再加一个站点组</button>
        </div>
        <div className="keyword-group-grid">
          <label className="settings-field"><span className="field-label">数据月份</span>
            <input aria-label="数据月份" placeholder="202607（留空为执行当月）" value={form.dataMonth}
              onChange={event=>edit(()=>({dataMonth:event.target.value}))}/>
            <small className="field-help">格式 yyyyMM，例如 202607。</small>
          </label>
          <label className="settings-field"><span className="field-label">矩阵月份</span>
            <input aria-label="矩阵月份" placeholder="留空跟随数据月份" value={form.matrixMonth}
              disabled={!form.matrixEnabled} onChange={event=>edit(()=>({matrixMonth:event.target.value}))}/>
            <small className="field-help">仅矩阵启用时提交，格式同为 yyyyMM。</small>
          </label>
        </div>
        <label className="checkline"><input type="checkbox" checked={form.matrixEnabled}
          onChange={event=>edit(()=>({matrixEnabled:event.target.checked}))}/>生成 ASIN × 关键词矩阵</label>
        <div className="keyword-group-grid"><label className="settings-field"><span className="field-label">任务文本模型</span><select value={modelRef} onChange={event=>selectModel(event.target.value)}>{modelOptions.map(item=><option key={item.ref} value={item.ref}>{item.ref}</option>)}</select></label><label className="settings-field"><span className="field-label">模型 Key</span><select value={credentialId} onChange={event=>{setCredentialId(event.target.value);setPlan(null);setForm(current=>({...current,planHash:''}))}}>{credentialOptions.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div>
        <small className="field-help">任务冻结所选模型和凭据编号；选择变化后须重新核算。<button className="text-link" onClick={onSettings}>全局默认设置</button></small>
        <div className="button-row">
          <button className="primary" disabled={busy==='plan'} onClick={countBudget}>
            {busy==='plan'?'正在核算…':'核算预算'}</button>
          <span className="field-help">核算只计算调用量，不发起任何取数。</span>
        </div>
      </section>}

      <div className="keyword-column">
        {view==='new'&&<RunCard state={state} form={form} busy={busy} onStart={start} onCancel={cancel}
          onDownload={download} onRefresh={()=>readSnapshot(state.runId)}/>}
        {view==='new'&&<BudgetCard plan={plan}/>}
        {view==='new'&&<section className="keyword-card">
          <h3>SellerSprite 连接</h3><p>{credential?.configured?'凭据已配置':'尚未配置'} · 密钥只在总设置中管理。</p><button onClick={onSettings}>管理 SellerSprite 凭据</button>
        </section>}
    {view==='queue'&&<section className="keyword-card keyword-list"><div className="list-heading"><div><h3>持久任务队列</h3><p>逐项执行；失败或取消后继续。重启后需要手动继续。</p></div><button onClick={continueQueue}>开始 / 继续队列</button><button onClick={stopQueue}>停止队列</button></div>{queue.items.map((item,index)=>{const id=item.run_id||item.id;return <div className="queue-item" key={id}><b>{index+1}</b><span>{item.display_name||id}</span><small>{item.status||'created'} · {item.stage||'等待执行'}</small><button disabled={!['created','queued'].includes(item.status)||id===queue.current} onClick={()=>reorder(id,-1)}>↑</button><button disabled={!['created','queued'].includes(item.status)||id===queue.current} onClick={()=>reorder(id,1)}>↓</button><button onClick={()=>{setView('detail');onSelectRun(id);readSnapshot(id)}}>详情</button></div>})}<h3>所有任务 <label className="checkline"><input type="checkbox" checked={showHidden} onChange={event=>setShowHidden(event.target.checked)}/>显示隐藏任务</label></h3><button disabled={!selected.length} onClick={()=>enqueue(selected)}>将所选任务排队（{selected.length}）</button>{recent.map((item,index)=>{const id=item.run_id||item.id;return <article className="keyword-task-row" key={id}><input type="checkbox" checked={selected.includes(id)} disabled={!canQueueStatus(item.status)} onChange={()=>setSelected(xs=>xs.includes(id)?xs.filter(x=>x!==id):[...xs,id])}/><span className={'keyword-state '+item.status}>{LABEL[item.status]||item.status}</span><button onClick={()=>{setView('detail');onSelectRun(id);readSnapshot(id)}}>{item.display_name||id}</button><small>{item.marketplaces?.join('、')||item.marketplace||'—'} · {item.asin_count??0} ASIN / {item.node_count??0} Node · {item.created_at||'—'} · 队列 {item.queue_position??'—'} · 尝试 {item.attempt??0}</small><div className="task-actions"><button onClick={()=>rename(item)}>重命名</button><button onClick={()=>hide(item,!!item.hidden)}>{item.hidden?'恢复':'隐藏'}</button><button onClick={()=>openDirectory(item)}>打开目录</button><button disabled={isActiveStatus(item.status)} onClick={()=>trash(item)}>移入回收站</button></div>{['failed','cancelled','interrupted'].includes(item.status)&&<button onClick={()=>enqueue([id])}>缓存续跑</button>}</article>})}{nextCursor&&<button onClick={()=>{setCursor(nextCursor);refreshRecent(nextCursor)}}>加载更多</button>}</section>}
        {view==='detail'&&<section className="keyword-card">
          <h3>{recent.find(x=>(x.run_id||x.id)===state.runId)?.display_name||'任务详情'} · {LABEL[state.status]||state.status}</h3>
          <p>阶段：{state.stage||'—'} · 队列位置：{state.queuePosition??'—'} · 尝试：{state.attempt??0}</p>
          {state.progress ? <div className="keyword-progress"><b>{state.progress.stage||state.stage}</b><span>{state.progress.done}{state.progress.total!==null&&state.progress.total!==undefined?' / '+state.progress.total:''} {state.progress.unit||''}</span>{state.progress.total>0 ? <div className="keyword-bar"><i style={{width:Math.min(100,Math.round(state.progress.done/state.progress.total*100))+'%'}}/></div> : null}<small>{state.progress.message||'当前阶段活动中'}</small></div> : null}
          <h4>最近活动</h4><div className="recent-activity">{state.recentActivity.length?state.recentActivity.map((item,index)=><p key={item.sequence||index}><time>{item.at?new Date(item.at).toLocaleTimeString():''}</time>{activityText(item)}</p>):<p>{state.progress?.message||state.message||'暂无活动记录'}</p>}</div>
          {state.error&&<p className="keyword-error">{state.error}</p>}
          <div className="button-row"><button onClick={()=>readSnapshot(state.runId)}>刷新详情</button><button disabled={!isActiveStatus(state.status)} onClick={cancel}>取消任务</button><button disabled={!['failed','cancelled','interrupted'].includes(state.status)} onClick={()=>enqueue([state.runId])}>缓存续跑</button><button onClick={()=>openDirectory(recent.find(x=>(x.run_id||x.id)===state.runId)||{run_id:state.runId})}>打开目录</button></div>
          <ol className="keyword-timeline">{state.timeline.map((item,index)=><li key={index} className={item.status}><b>{item.stage}</b><span>{item.message||item.status}</span><small>{item.at||item.finished_at||''}</small></li>)}</ol>
          {state.artifacts.map(item=><div className="keyword-artifact" key={item.name}><span>{item.name}</span><small>{bytesText(item.bytes)}</small><button onClick={()=>download(item.name)}><Download size={14}/>下载</button></div>)}
          <details className="keyword-log" open={isActiveStatus(state.status)}><summary>完整过程日志 · {state.log.length} 条</summary>{state.log.map(item=><p key={item.sequence}><span>{item.sequence}</span>{item.line}</p>)}</details>
        </section>}
        {view==='new'&&<section className="keyword-card">
          <h3>历史运行</h3>
          {recent.length?recent.map(item=><div className="keyword-recent" key={item.run_id}>
            <span className={'keyword-state '+item.status}>{LABEL[item.status]||item.status}</span>
            <span>{item.run_id.slice(0,12)}…</span>
            <small>{item.stage||'—'}{item.created_at?' · '+item.created_at:''}</small>
            <button onClick={()=>readSnapshot(item.run_id)}>查看</button>
          </div>):<p className="field-help">还没有运行记录。</p>}
        </section>}
      </div>
    </div>
  </div>;
}
