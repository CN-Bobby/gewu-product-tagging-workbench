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
import{primaryCredentialForModel}from'./modelCredential';
import{ENDPOINTS,SSE_EVENT_NAME,buildRunRequest,initialKeywordForm,initialKeywordState,
  isActiveStatus,isCurrentRunSnapshot,isTerminalEvent,isTerminalStatus,keywordReducer,parseAsins,parseNodeIds,pendingQueueCount,summarizeActivity,validQueueSelection}
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
const QUEUE_STATE_LABEL={idle:'待命',running:'执行中',stopped:'已暂停'};
const canQueueStatus=status=>['created','failed','cancelled','interrupted','skipped'].includes(status);

const bytesText=value=>{
  if(value==null||!Number.isFinite(Number(value)))return'—';
  const size=Number(value);
  return size<1024?size+' B':(size/1024).toFixed(1)+' KB';
};

function GroupFields({group,index,meta,onChange,onRemove,canRemove}){
  const parsed=parseAsins(group.asinsText);
  const nodes=parseNodeIds(group.nodeIdsText);
  const mode=group.mode==='node'?'node':'asin';
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
    </div>
    <fieldset className="keyword-input-mode"><legend>取数方式</legend>
      <label><input type="radio" name={'input-mode-'+group.key} checked={mode==='asin'} onChange={()=>onChange({mode:'asin'})}/>自定义 ASIN 范围</label>
      <label><input type="radio" name={'input-mode-'+group.key} checked={mode==='node'} onChange={()=>onChange({mode:'node'})}/>Node 榜单</label>
    </fieldset>
    {mode==='asin'?<label className="settings-field"><span className="field-label">ASIN</span>
      <textarea aria-label={'站点组 '+(index+1)+' ASIN'} value={group.asinsText} placeholder="每行一个，或用逗号、分号、顿号分隔"
        onChange={event=>onChange({asinsText:event.target.value})}/>
      <small className="field-help">只按这些 ASIN 反查，不触发 Node 验证或榜单取数。已识别 {parsed.valid.length} 个（去重后）；单站点组最多 20 个。{parsed.invalid.length>0?' 未识别：'+parsed.invalid.join('、'):''}</small>
    </label>:<div className="keyword-group-grid keyword-node-fields">
      <label className="settings-field"><span className="field-label">Node ID</span>
        <input aria-label={'站点组 '+(index+1)+' Node ID'} value={group.nodeIdsText} placeholder="输入一个类目节点 ID"
          onChange={event=>onChange({nodeIdsText:event.target.value})}/>
        <small className="field-help">每个站点组填写一个 Node；需要多个 Node 时新增站点组。已识别 {nodes.length} 个。</small>
      </label>
      <label className="settings-field"><span className="field-label">榜单取数条数</span>
        <select aria-label={'站点组 '+(index+1)+' 榜单取数条数'} value={group.nodeTopN} onChange={event=>onChange({nodeTopN:Number(event.target.value)})}>
          {(meta?meta.node_top_n_options:[]).map(value=><option key={value} value={value}>Top {value}</option>)}
        </select>
        <small className="field-help">只对这个 Node 取对应数量。</small>
      </label>
    </div>}
    {mode==='node'&&nodes.length>1&&<p className="keyword-input-warning" role="alert">一个站点组只能有一个 Node ID，请拆分为多个站点组。</p>}
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

export function KeywordResearch({api,selectedRunId='',onSelectRun=()=>{},onSettings=()=>{}}){
  const [projects,setProjects]=useState([]);
  const [workspaceId,setWorkspaceId]=useState('');
  const [meta,setMeta]=useState(null);
  const [credential,setCredential]=useState(null);
  const [form,setForm]=useState(initialKeywordForm);
  const [state,dispatch]=useReducer(keywordReducer,initialKeywordState);
  const selectedRunRef=useRef(selectedRunId||'');
  const [plan,setPlan]=useState(null);
  const [errors,setErrors]=useState([]);
  const [recent,setRecent]=useState([]),[queue,setQueue]=useState({items:[],state:'idle',dispatch_enabled:false}),[nextCursor,setNextCursor]=useState(null),[showHidden,setShowHidden]=useState(false),[view,setView]=useState(selectedRunId?'queue':location.hash.startsWith('#/keyword/queue')?'queue':'new'),[selected,setSelected]=useState([]),[queueConfirmation,setQueueConfirmation]=useState(null),[selectedTaskId,setSelectedTaskId]=useState(selectedRunId||''),[notice,setNotice]=useState(''),[displayName,setDisplayName]=useState(''),[models,setModels]=useState(null),[modelRef,setModelRef]=useState(''),[credentialId,setCredentialId]=useState('');
  const [busy,setBusy]=useState('');
  const stream=useRef(null),latest=useRef(state);
  latest.current=state;

  useEffect(()=>{let cancelled=false;
    api('/api/projects',undefined,'GET').then(value=>{if(!cancelled)setProjects(Array.isArray(value)?value:[])}).catch(()=>{});
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
    api('/api/settings/models',undefined,'GET').then(value=>{if(cancelled)return;setModels(value);const initial=value.defaults?.[0]||value.providers?.flatMap(p=>p.model_refs||[])[0]||'';setModelRef(initial);setCredentialId(primaryCredentialForModel(value,initial))}).catch(error=>{if(!cancelled)setErrors([error.message])});
    return()=>{cancelled=true};
  },[]);

  const refreshRecent=(next='',append=false)=>api(`${ENDPOINTS.runs()}?limit=50${next?'&cursor='+encodeURIComponent(next):''}${showHidden?'&hidden=true':''}`,undefined,'GET')
    .then(data=>{const runs=data.runs||[];setRecent(current=>append?[...new Map([...current,...runs].map(item=>[item.run_id||item.id,item])).values()]:runs);setNextCursor(data.next_cursor||null);return runs})
    .catch(error=>{setErrors([error.message]);return null});

  const refreshQueue=()=>api(ENDPOINTS.queue(),undefined,'GET').then(value=>{
    const normalized={...value,items:value.items||value.queue||[]};setQueue(normalized);return normalized;
  }).catch(error=>{setErrors([error.message]);return null});
  useEffect(()=>{refreshRecent('');refreshQueue()},[showHidden]);
  useEffect(()=>{
    if(view!=='queue')return undefined;
    const refreshVisible=()=>{if(document.visibilityState!=='hidden'){refreshRecent('');refreshQueue()}};
    const timer=setInterval(refreshVisible,5000);
    window.addEventListener('focus',refreshVisible);
    return()=>{clearInterval(timer);window.removeEventListener('focus',refreshVisible)};
  },[view,showHidden]);
  useEffect(()=>{
    const eligible=new Set([...recent,...queue.items].filter(item=>canQueueStatus(item.status)).map(item=>item.run_id||item.id));
    for(const item of queue.items)if(['queued','running','cancelling'].includes(item.status))eligible.delete(item.run_id||item.id);
    setSelected(current=>current.filter(id=>eligible.has(id)));
  },[recent,queue.items]);
  useEffect(()=>{
    if(selectedRunId){selectedRunRef.current=selectedRunId;setView('queue');setSelectedTaskId(selectedRunId);dispatch({type:'run_selected',runId:selectedRunId});readSnapshot(selectedRunId)}
    else{selectedRunRef.current='';setSelectedTaskId('');dispatch({type:'run_selected',runId:null})}
  },[selectedRunId]);
  useEffect(()=>{
    if(view!=='queue'||selectedTaskId||selectedRunId)return;
    const latest=recent[0],running=queue.items.find(item=>item.run_id===queue.current||item.id===queue.current);
    const id=queue.current||latest?.run_id||latest?.id||running?.run_id||running?.id;
    if(id){setSelectedTaskId(id);onSelectRun(id)}
  },[view,recent,queue.items,queue.current,selectedTaskId,selectedRunId]);

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
        if(isTerminalEvent(event&&event.event_type)){
          source.close();
          readSnapshot(state.runId);
        }
      });
      source.onerror=()=>{
        if(closed||source===null)return;
        const dropped=source;source=null;dropped.close();
        readSnapshot(state.runId).then(snapshot=>{
          if(!closed&&snapshot&&!isTerminalStatus(snapshot.status))connect();
        });
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
    groups:current.groups.map(group=>group.key===key?{...group,...(typeof mutator==='function'?mutator(group):mutator)}:group),
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

  const start=async(dispatchMode='enqueue')=>{
    setBusy('start');
    try{
      if(!plan?.plan_hash||!form.planHash)throw Error('请先核算预算');
      const name=displayName.trim()||`关键词调研 ${new Date().toLocaleString()}`;
      const chosen=plan.selected||plan;
      const created=await callJson(ENDPOINTS.runs(),{...buildRunRequest(form),dispatch:dispatchMode,display_name:name,workspace_id:workspaceId||null,model_ref:chosen.model_ref||modelRef,credential_id:chosen.credential_id||credentialId});
      selectedRunRef.current=created.run_id;
      dispatch({type:'run_created',runId:created.run_id,status:created.status});setSelectedTaskId(created.run_id);setNotice(`“${name}”${dispatchMode==='create'?'已保存':'已创建并提交队列'} · ${LABEL[created.status]||created.status}`);setErrors([]);setView('queue');await Promise.all([refreshRecent(''),refreshQueue()]);onSelectRun(created.run_id);await readSnapshot(created.run_id);
    }catch(error){setErrors(error.errors||[error.message])}
    finally{setBusy('')}
  };

  const linkProject=async value=>{
    if(!state.runId)return;
    try{
      await api(`${ENDPOINTS.run(state.runId)}/project`,{workspace_id:value||null},'PATCH');
      await readSnapshot(state.runId);
      await Promise.all([refreshRecent(''),refreshQueue()]);
      setNotice(value?'已关联项目，完成后的关键词文件会进入项目资料。':'已取消项目关联。');
    }catch(error){setErrors([error.message])}
  };

  const freshQueueSelection=async ids=>{
    const selectedIds=[...new Set(ids||[])];
    const query=cursor=>`${ENDPOINTS.runs()}?limit=50${cursor?'&cursor='+encodeURIComponent(cursor):''}${showHidden?'&hidden=true':''}`;
    const [first,freshQueue]=await Promise.all([api(query(''),undefined,'GET'),api(ENDPOINTS.queue(),undefined,'GET')]);
    const normalized={...freshQueue,items:freshQueue.items||freshQueue.queue||[]};
    const allRuns=[...(first.runs||[])];let cursor=first.next_cursor||null,firstCursor=cursor,passes=0;
    while(cursor&&selectedIds.some(id=>!allRuns.some(item=>(item.run_id||item.id)===id))){
      if(++passes>100)throw Error('任务列表分页过多，已中止排队；请缩小选择范围后重试。');
      const page=await api(query(cursor),undefined,'GET');allRuns.push(...(page.runs||[]));
      if(page.next_cursor===cursor)throw Error('任务列表游标未前进，已中止排队。');
      cursor=page.next_cursor||null;
    }
    const firstRuns=first.runs||[],firstIds=new Set(firstRuns.map(item=>item.run_id||item.id));
    const refreshedById=new Map([...allRuns,...normalized.items].map(item=>[item.run_id||item.id,item]));
    setRecent(current=>[...firstRuns,...current.filter(item=>!firstIds.has(item.run_id||item.id)).map(item=>refreshedById.get(item.run_id||item.id)||item)]);
    setNextCursor(firstCursor);setQueue(normalized);
    return validQueueSelection(selectedIds,allRuns,normalized.items);
  };

  const enqueue=async ids=>{
    setBusy('queue');setErrors([]);setQueueConfirmation(null);
    try{
      const selection=await freshQueueSelection(ids);
      if(selection.stale.length){setSelected(selection.valid);throw Error('任务状态已变化，失效或已排队的勾选项已移除。请检查列表后重试。')}
      if(!selection.valid.length)return;
      const names=selection.items.map(item=>item.display_name||item.name||item.run_id||item.id);
      const total=selection.items.reduce((sum,item)=>sum+Number(item.budget?.planned_total??item.planned_total??item.planned_calls??0),0);
      setQueueConfirmation({ids:selection.valid,names,total});
    }catch(error){setErrors(error.errors||[error.message])}finally{setBusy('')}
  };

  const confirmEnqueue=async()=>{
    if(!queueConfirmation)return;
    setBusy('queue');setErrors([]);
    try{
      const latest=await freshQueueSelection(queueConfirmation.ids);
      if(latest.stale.length){setSelected(latest.valid);setQueueConfirmation(null);throw Error('确认期间任务状态已变化，已移除失效勾选项；请检查后重新确认。')}
      if(!latest.valid.length){setQueueConfirmation(null);return}
      await callJson(ENDPOINTS.queue(),{run_ids:latest.valid});setQueueConfirmation(null);setErrors([]);setSelected([]);await Promise.all([refreshQueue(),refreshRecent('')]);
      if(state.runId&&latest.valid.includes(state.runId))await readSnapshot(state.runId);
    }catch(error){setErrors(error.errors||[error.message])}finally{setBusy('')}
  };

  const cancel=async()=>{
    setBusy('cancel');
    try{await api(ENDPOINTS.runCancel(state.runId),{})}
    catch(error){setErrors([error.message])}
    finally{setBusy('')}
  };

  const readSnapshot=async runId=>{
    try{
      const snapshot=await api(ENDPOINTS.run(runId),undefined,'GET');
      if(!isCurrentRunSnapshot(runId,selectedRunRef.current))return null;
      dispatch({type:'snapshot',snapshot});
      if(isTerminalStatus(snapshot?.status)){
        const[,latestQueue]=await Promise.all([refreshRecent(''),refreshQueue()]);
        // The run can emit its terminal snapshot just before the scheduler releases `current`.
        // One bounded follow-up reconciles that narrow window without reconnecting SSE or polling.
        if(latestQueue?.current===runId)setTimeout(()=>{refreshRecent('');refreshQueue()},500);
      }
      return snapshot;
    }catch(error){setErrors([error.message]);return null}
  };

  const selectTask=id=>{
    if(!id)return;
    selectedRunRef.current=id;
    setSelectedTaskId(id);setView('queue');setNotice('');onSelectRun(id);
  };

  const modelOptions=(models?.providers||[]).flatMap(provider=>(provider.model_refs||[]).map(ref=>({ref,provider})));
  const selectModel=value=>{setModelRef(value);setCredentialId(primaryCredentialForModel(models,value));setPlan(null);setForm(current=>({...current,planHash:''}))};
  const reorder=async(id,offset)=>{const items=(queue.items||[]).filter(item=>['created','queued'].includes(item.status)&&item.run_id!==queue.current&&item.id!==queue.current),index=items.findIndex(item=>(item.run_id||item.id)===id),next=index+offset;if(index<0||next<0||next>=items.length)return;const ids=items.map(item=>item.run_id||item.id);[ids[index],ids[next]]=[ids[next],ids[index]];try{await api(ENDPOINTS.queueOrder(),{run_ids:ids},'PUT');await refreshQueue()}catch(error){setErrors([error.message])}};
  const stopQueue=async()=>{if(!window.confirm('停止当前关键词队列？当前任务将取消，尚未开始的任务标记为跳过并保留输入与缓存。'))return;try{await api(ENDPOINTS.queueStop(),{});await refreshQueue();await refreshRecent('')}catch(error){setErrors([error.message])}};
  const continueQueue=async()=>{try{await api(ENDPOINTS.queueContinue(),{});await Promise.all([refreshQueue(),refreshRecent('')]);if(state.runId)await readSnapshot(state.runId)}catch(error){setErrors([error.message])}};
  const rename=async item=>{const id=item.run_id||item.id,name=window.prompt('任务名称',item.display_name||item.name||'关键词调研');if(!name?.trim())return;try{await api(ENDPOINTS.run(id),{display_name:name.trim()},'PATCH');await refreshRecent('')}catch(error){setErrors([error.message])}};
  const hide=async(item,hidden)=>{const id=item.run_id||item.id;try{await api(hidden?ENDPOINTS.runUnhide(id):ENDPOINTS.runHide(id),{});if(!hidden){setRecent(current=>current.filter(value=>(value.run_id||value.id)!==id));setSelected(current=>current.filter(value=>value!==id));setQueueConfirmation(null)}await Promise.all([refreshRecent(''),refreshQueue()])}catch(error){setErrors([error.message])}};
  const trash=async item=>{const id=item.run_id||item.id;if(isActiveStatus(item.status)||['created','queued'].includes(item.status)&&queue.items.some(q=>(q.run_id||q.id)===id&&q.current))return setErrors(['运行中的任务不能移入回收站']);const contents=item.artifacts?.length?item.artifacts.map(file=>file.name).join('、'):'任务输入、缓存、日志与已生成产物';if(!window.confirm(`确认将“${item.display_name||item.name||id}”及其运行目录移入 Windows 回收站？范围：${contents}。失败时会保留任务。`))return;try{await callJson(ENDPOINTS.run(id),{},'DELETE');setRecent(current=>current.filter(value=>(value.run_id||value.id)!==id));setSelected(current=>current.filter(value=>value!==id));setQueueConfirmation(null);await Promise.all([refreshRecent(''),refreshQueue()])}catch(error){setErrors(error.errors||[error.message])}};
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

  const parsedTotal=form.groups.reduce((sum,group)=>sum+(group.mode==='node'?0:parseAsins(group.asinsText).valid.length),0);
  const nodeTotal=form.groups.reduce((sum,group)=>sum+(group.mode==='node'?parseNodeIds(group.nodeIdsText).length:0),0);
  const invalidNodeGroups=form.groups.filter(group=>group.mode==='node'&&parseNodeIds(group.nodeIdsText).length!==1);
  const resumableQueue=queue.items.some(item=>['queued','interrupted'].includes(item.status));
  const queueStateLabel=QUEUE_STATE_LABEL[queue.state]||'待命';
  const pendingQueueItems=pendingQueueCount(queue.items);
  const showProgress=state.progress&&['running','cancelling'].includes(state.status);
  const activityText=item=>{const payload=item?.payload||{};return [item?.event_type?.replaceAll('_',' '),payload.stage,payload.message||payload.line].filter(Boolean).join(' · ')};

  return <div className="keyword-page">
    <header className="keyword-heading">
      <div>
        <div className="eyebrow">KEYWORD RESEARCH</div>
        <h1>关键词调研<span>输入范围、确认预算，然后开始执行</span></h1>
      </div>
      <div className="button-row"><span className="keyword-summary">{form.groups.length} 个站点组 · {parsedTotal} 个 ASIN · {nodeTotal} 个 Node</span><button onClick={()=>{refreshRecent('');refreshQueue()}}><RefreshCw size={13}/>刷新</button></div>
    </header>
    <nav className="keyword-tabs"><button className={view==='new'?'active':''} onClick={()=>{selectedRunRef.current='';setSelectedTaskId('');dispatch({type:'run_selected',runId:null});setView('new');onSelectRun('')}}>新建任务</button><button className={view==='queue'?'active':''} onClick={()=>setView('queue')}>队列与任务</button><span>队列{queueStateLabel} · {pendingQueueItems} 个待执行</span></nav>

    {errors.length>0&&<div role="alert" className="keyword-errors">
      <div className="keyword-errors-head"><strong>有 {errors.length} 处需要处理</strong>
        <button onClick={()=>setErrors([])}><X size={15}/></button></div>
      <ul>{errors.map((message,index)=><li key={index}>{message}</li>)}</ul>
    </div>}
    {notice&&<p className="keyword-success" role="status">{notice}</p>}

    <div className={'keyword-grid keyword-view-'+view}>
      {view==='new'&&<section className="keyword-card">
        <label className="settings-field"><span className="field-label">任务名称</span><input value={displayName} onChange={event=>setDisplayName(event.target.value)} placeholder="例如：核心词调研"/></label>
        <label className="settings-field"><span className="field-label">保存到项目</span><select aria-label="保存到项目" value={workspaceId} onChange={event=>setWorkspaceId(event.target.value)}><option value="">暂不关联项目</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select><small className="field-help">运行完成后，Excel 结果会进入项目的“关键词研究”分类。</small></label>
        <h3>调研输入</h3>
        {form.groups.map((group,index)=><GroupFields key={group.key} group={group} index={index} meta={meta}
          canRemove={form.groups.length>1} onChange={patch=>editGroup(group.key,patch)}
          onRemove={()=>edit(current=>({groups:current.groups.filter(item=>item.key!==group.key)}))}/>)}
        <div className="button-row">
          <button onClick={()=>edit(current=>({groups:[...current.groups,{key:'g'+Date.now(),
            marketplace:(meta&&meta.marketplaces&&meta.marketplaces[0])||'',mode:'asin',asinsText:'',nodeIdsText:'',
            nodeTopN:(meta&&meta.node_top_n_options&&meta.node_top_n_options[0])||50}]}))}>
            <Plus size={14}/>再加一个站点组</button>
        </div>
        <details className="keyword-advanced"><summary>高级选项 · 月份、矩阵、模型与凭据</summary>
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
        <div className="keyword-group-grid"><label className="settings-field"><span className="field-label">任务文本模型</span><select value={modelRef} onChange={event=>selectModel(event.target.value)}>{modelOptions.map(item=><option key={item.ref} value={item.ref}>{item.ref}</option>)}</select></label></div>
        <small className="field-help">任务自动使用该服务在总设置中设为当前的 Key，并冻结模型和凭据编号。选择模型后须重新核算。<button className="text-link" onClick={onSettings}>打开总设置</button></small>
        </details>
        <div className="button-row">
          <button className="primary" disabled={busy==='plan'||invalidNodeGroups.length>0} onClick={countBudget}>
            {busy==='plan'?'正在核算…':'核算预算'}</button>
          <span className="field-help">核算只计算调用量，不发起任何取数。</span>
          {invalidNodeGroups.length>0&&<small className="keyword-input-warning">Node 榜单模式下每个站点组必须填写且只填写一个 Node ID。</small>}
        </div>
      </section>}

      <div className="keyword-column">
        {view==='new'&&<BudgetCard plan={plan}/>}
        {view==='new'&&plan?.plan_hash&&plan.plan_hash===form.planHash&&<section className="keyword-card keyword-confirm-card">
          <h3>预算与执行</h3><p>任务：<b>{displayName.trim()||'关键词调研（自动命名）'}</b> · 预计 {plan.budget?.planned_total??0} 次 SellerSprite 调用。</p>
          <p className="field-help">选择下方动作后立即创建。开始执行会提交队列；仅保存会保持“已创建”状态。</p>
          <div className="button-row"><button className="primary" disabled={busy||invalidNodeGroups.length>0} onClick={()=>start('enqueue')}><Play size={14}/>创建并开始执行</button><button disabled={busy||invalidNodeGroups.length>0} onClick={()=>start('create')}>仅保存任务</button></div>
        </section>}
        {view==='new'&&<section className="keyword-card">
          <h3>SellerSprite 连接</h3><p>{credential?.configured?'凭据已配置':'尚未配置'} · 密钥只在总设置中管理。</p><button onClick={onSettings}>管理 SellerSprite 凭据</button>
        </section>}
    {view==='queue'&&<div className="keyword-workbench-split">
      <section className="keyword-task-list-pane keyword-card keyword-list">
        <div className="list-heading"><div><h3>队列</h3><p>{queue.current?'当前任务正在执行；其他任务会依次开始。':queue.dispatch_enabled?'队列已启动，任务会依次自动执行。':'入队后会自动开始；工作台重启后需手动继续。'}</p></div>{!queue.dispatch_enabled&&!queue.current&&resumableQueue&&<button onClick={continueQueue}>继续待执行任务</button>}{queue.dispatch_enabled&&<button onClick={stopQueue}>停止队列</button>}</div>
        {queue.items.length?queue.items.map((item,index)=>{const id=item.run_id||item.id;return <div className={'queue-item '+(selectedTaskId===id?'selected':'')} key={id} onClick={()=>selectTask(id)}><b>{index+1}</b><button onClick={event=>{event.stopPropagation();selectTask(id)}}>{item.display_name||id}</button><small>{LABEL[item.status]||item.status} · {item.stage||'等待执行'}</small><button disabled={!['created','queued'].includes(item.status)||id===queue.current} onClick={event=>{event.stopPropagation();reorder(id,-1)}}>↑</button><button disabled={!['created','queued'].includes(item.status)||id===queue.current} onClick={event=>{event.stopPropagation();reorder(id,1)}}>↓</button></div>}):<p className="field-help">队列为空。</p>}
        <h3>任务 <label className="checkline"><input type="checkbox" checked={showHidden} onChange={event=>setShowHidden(event.target.checked)}/>显示隐藏任务</label></h3>
        <button disabled={!selected.length||busy==='queue'} onClick={()=>enqueue(selected)}>核对所选任务</button>
        {queueConfirmation&&<section className="keyword-confirm-card" role="group" aria-label="排队确认">
          <h4>确认开始 {queueConfirmation.ids.length} 项任务</h4><ol>{queueConfirmation.names.map((name,index)=><li key={queueConfirmation.ids[index]}>{name}</li>)}</ol>
          <p>合计预算：{queueConfirmation.total} 次取数调用。确认后按当前队列顺序执行。</p>
          <div className="button-row"><button className="primary" disabled={busy==='queue'} onClick={confirmEnqueue}>确认并开始执行</button><button disabled={busy==='queue'} onClick={()=>setQueueConfirmation(null)}>取消</button></div>
        </section>}
        {recent.map(item=>{const id=item.run_id||item.id;return <article className={'keyword-task-row '+(selectedTaskId===id?'selected':'')} key={id} onClick={()=>selectTask(id)}>
          <input type="checkbox" aria-label={'选择任务 '+(item.display_name||id)} checked={selected.includes(id)} disabled={!canQueueStatus(item.status)} onClick={event=>event.stopPropagation()} onChange={()=>{setQueueConfirmation(null);setSelected(xs=>xs.includes(id)?xs.filter(x=>x!==id):[...xs,id])}}/>
          <span className={'keyword-state '+item.status}>{LABEL[item.status]||item.status}</span><button onClick={event=>{event.stopPropagation();selectTask(id)}}>{item.display_name||id}</button>
          <small>{item.marketplaces?.join('、')||item.marketplace||'—'} · {item.asin_count??0} ASIN / {item.node_count??0} Node · {item.created_at||'—'} · 队列 {item.queue_position??'—'} · 尝试 {item.attempt??0}</small>
          <div className="task-actions"><button onClick={event=>{event.stopPropagation();rename(item)}}>重命名</button><button onClick={event=>{event.stopPropagation();hide(item,!!item.hidden)}}>{item.hidden?'恢复':'隐藏'}</button><button onClick={event=>{event.stopPropagation();openDirectory(item)}}>打开目录</button><button disabled={isActiveStatus(item.status)} onClick={event=>{event.stopPropagation();trash(item)}}>移入回收站</button></div>
          {['failed','cancelled','interrupted'].includes(item.status)&&<button onClick={event=>{event.stopPropagation();enqueue([id])}}>缓存续跑</button>}
        </article>})}{nextCursor&&<button onClick={()=>refreshRecent(nextCursor,true)}>加载更多任务</button>}
      </section>
      <section className="keyword-process-pane keyword-card">
        {state.runId?<>
          <h3>{recent.find(x=>(x.run_id||x.id)===state.runId)?.display_name||queue.items.find(x=>(x.run_id||x.id)===state.runId)?.display_name||'任务详情'} · {LABEL[state.status]||state.status}</h3>
          <p>阶段：{state.stage||'—'} · 队列位置：{state.queuePosition??'—'} · 尝试：{state.attempt??0}</p>
          <label className="settings-field"><span className="field-label">所属项目</span><select aria-label="所属项目" value={state.workspaceId||''} disabled={isActiveStatus(state.status)} onChange={event=>linkProject(event.target.value)}><option value="">未关联</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select><small className="field-help">未关联的历史任务可在此手动归档到项目。执行中的任务不可修改。</small></label>
          {showProgress ? <div className="keyword-progress"><b>{state.progress.stage||state.stage||'执行中'}</b><span>{state.progress.done??'—'}{state.progress.total!==null&&state.progress.total!==undefined?' / '+state.progress.total:''} {state.progress.unit||''}</span>{state.progress.total>0 ? <div className="keyword-bar"><i style={{width:Math.min(100,Math.round(state.progress.done/state.progress.total*100))+'%'}}/></div> : null}{state.progress.message&&<small>{state.progress.message}</small>}</div> : <p>当前阶段：{state.stage||state.message||'等待任务更新'}</p>}
          <h4>最近活动</h4><div className="recent-activity">{summarizeActivity(state.recentActivity).length?summarizeActivity(state.recentActivity).map((item,index)=><p key={item.sequence||index}><time>{item.at?new Date(item.at).toLocaleTimeString():''}</time>{item.text}</p>):<p>{state.progress?.message||state.message||'暂无活动记录'}</p>}</div>
          {state.error&&<p className="keyword-error">{state.error}</p>}
          <div className="button-row"><button onClick={()=>readSnapshot(state.runId)}>刷新详情</button><button disabled={!isActiveStatus(state.status)} onClick={cancel}>取消任务</button><button disabled={!['failed','cancelled','interrupted'].includes(state.status)} onClick={()=>enqueue([state.runId])}>缓存续跑</button><button onClick={()=>openDirectory(recent.find(x=>(x.run_id||x.id)===state.runId)||{run_id:state.runId})}>打开目录</button></div>
          <h4>产物</h4>{state.artifacts.length?state.artifacts.map(item=><div className="keyword-artifact" key={item.name}><span>{item.name}</span><small>{bytesText(item.bytes)}</small><button onClick={()=>download(item.name)}><Download size={14}/>下载</button></div>):<p className="field-help">暂无产物。</p>}
          <ol className="keyword-timeline">{state.timeline.map((item,index)=><li key={index} className={item.status}><b>{item.stage}</b><span>{item.message||item.status}</span><small>{item.at||item.finished_at||''}</small></li>)}</ol>
          <details className="keyword-log"><summary>完整过程日志 · {state.log.length} 条</summary>{state.log.map(item=><p key={item.sequence}><span>{item.sequence}</span>{item.line}</p>)}</details>
        </>:<><h3>选择一个任务</h3><p className="field-help">点击左侧队列或任务查看状态、进度、活动和产物。</p></>}
      </section>
    </div>}
        {view==='new'&&<section className="keyword-card">
          <h3>历史运行</h3>
          {recent.length?recent.map(item=><div className="keyword-recent" key={item.run_id}>
            <span className={'keyword-state '+item.status}>{LABEL[item.status]||item.status}</span>
            <span>{item.run_id.slice(0,12)}…</span>
            <small>{item.stage||'—'}{item.created_at?' · '+item.created_at:''}</small>
            <button onClick={()=>selectTask(item.run_id)}>查看</button>
          </div>):<p className="field-help">还没有运行记录。</p>}
        </section>}
      </div>
    </div>
  </div>;
}
