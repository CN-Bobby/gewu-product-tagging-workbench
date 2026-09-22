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
  queued:'排队中',running:'执行中',cancelling:'正在取消',done:'已完成',
  failed:'失败',cancelled:'已取消',interrupted:'已中断',idle:'尚未发起',
};

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
      <button className="primary" disabled={!form.planHash||busy==='start'||active} onClick={onStart}>
        <Play size={14}/>{active?'已有运行在跑':'确认发起'}</button>
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

export function KeywordResearch({api}){
  const [meta,setMeta]=useState(null);
  const [credential,setCredential]=useState(null);
  const [secret,setSecret]=useState('');
  const [form,setForm]=useState(initialKeywordForm);
  const [state,dispatch]=useReducer(keywordReducer,initialKeywordState);
  const [plan,setPlan]=useState(null);
  const [errors,setErrors]=useState([]);
  const [recent,setRecent]=useState([]);
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
    return()=>{cancelled=true};
  },[]);

  const refreshRecent=()=>api(ENDPOINTS.runs(),undefined,'GET')
    .then(data=>setRecent(data.runs||[])).catch(()=>{});

  useEffect(()=>{refreshRecent()},[]);

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
      const counted=await callJson(ENDPOINTS.plan(),buildRunRequest(form));
      setPlan(counted);setErrors([]);
      setForm(current=>({...current,planHash:counted.plan_hash||''}));
    }catch(error){setPlan(null);setForm(current=>({...current,planHash:''}));setErrors(error.errors||[error.message])}
    finally{setBusy('')}
  };

  const start=async()=>{
    setBusy('start');
    try{
      const created=await callJson(ENDPOINTS.runs(),buildRunRequest(form));
      dispatch({type:'run_created',runId:created.run_id});
      setErrors([]);refreshRecent();
      // First paint of the new run is the snapshot, exactly like a reconnect: the stream only pushes.
      await readSnapshot(created.run_id);
    }catch(error){setErrors(error.errors||[error.message])}
    finally{setBusy('')}
  };

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

  const saveSecret=async()=>{
    setBusy('credential');
    try{setCredential(await api(ENDPOINTS.credentials(),{secret_key:secret}));setSecret('');setErrors([])}
    catch(error){setErrors([error.message])}
    finally{setBusy('')}
  };

  const clearSecret=async()=>{
    setBusy('credential');
    try{setCredential(await api(ENDPOINTS.credentials(),{},'DELETE'));setErrors([])}
    catch(error){setErrors([error.message])}
    finally{setBusy('')}
  };

  const parsedTotal=form.groups.reduce((sum,group)=>sum+parseAsins(group.asinsText).valid.length,0);
  const nodeTotal=form.groups.reduce((sum,group)=>sum+parseNodeIds(group.nodeIdsText).length,0);

  return <div className="keyword-page">
    <header className="keyword-heading">
      <div>
        <div className="eyebrow">KEYWORD RESEARCH</div>
        <h1>关键词调研<span>填站点组 → 核算预算 → 确认发起 → 下载 Excel</span></h1>
      </div>
      <div className="button-row"><span className="keyword-summary">{form.groups.length} 个站点组 · {parsedTotal} 个 ASIN · {nodeTotal} 个 Node</span></div>
    </header>

    {errors.length>0&&<div role="alert" className="keyword-errors">
      <div className="keyword-errors-head"><strong>有 {errors.length} 处需要处理</strong>
        <button onClick={()=>setErrors([])}><X size={15}/></button></div>
      <ul>{errors.map((message,index)=><li key={index}>{message}</li>)}</ul>
    </div>}

    <div className="keyword-grid">
      <section className="keyword-card">
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
        <div className="button-row">
          <button className="primary" disabled={busy==='plan'} onClick={countBudget}>
            {busy==='plan'?'正在核算…':'核算预算'}</button>
          <span className="field-help">核算只计算调用量，不发起任何取数。</span>
        </div>
      </section>

      <div className="keyword-column">
        <RunCard state={state} form={form} busy={busy} onStart={start} onCancel={cancel}
          onDownload={download} onRefresh={()=>readSnapshot(state.runId)}/>
        <BudgetCard plan={plan}/>
        <section className="keyword-card">
          <h3>SellerSprite 密钥</h3>
          <p className="field-help">密钥只写入本机凭据文件，保存后不再回显；留空不会覆盖已存的密钥。</p>
          <div className="keyword-run-head">
            <span className={'keyword-state '+(credential&&credential.configured?'done':'idle')}>
              {credential&&credential.configured?'已配置':'未配置'}</span>
            {credential&&credential.updated_at&&<span>更新于 {new Date(credential.updated_at).toLocaleString()}</span>}
          </div>
          <label className="settings-field"><span className="field-label">MCP 密钥</span>
            <input aria-label="SellerSprite MCP 密钥" type="password" autoComplete="new-password" value={secret}
              placeholder="只写不回显" onChange={event=>setSecret(event.target.value)}/>
            <small className="field-help">来自 SellerSprite MCP 的 secret key，不进入日志与运行记录。</small>
          </label>
          <div className="button-row">
            <button className="primary" disabled={busy==='credential'||!secret.trim()} onClick={saveSecret}>保存密钥</button>
            <button disabled={busy==='credential'||!(credential&&credential.configured)} onClick={clearSecret}>清除密钥</button>
          </div>
        </section>
        <section className="keyword-card">
          <h3>历史运行</h3>
          {recent.length?recent.map(item=><div className="keyword-recent" key={item.run_id}>
            <span className={'keyword-state '+item.status}>{LABEL[item.status]||item.status}</span>
            <span>{item.run_id.slice(0,12)}…</span>
            <small>{item.stage||'—'}{item.created_at?' · '+item.created_at:''}</small>
            <button onClick={()=>readSnapshot(item.run_id)}>查看</button>
          </div>):<p className="field-help">还没有运行记录。</p>}
          <div className="button-row"><button onClick={refreshRecent}><RefreshCw size={13}/>刷新列表</button></div>
        </section>
      </div>
    </div>
  </div>;
}
