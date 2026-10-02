import React,{useCallback,useEffect,useMemo,useRef,useState}from'react';
import{ArrowDownToLine,BookOpenCheck,CheckCircle2,CircleAlert,LoaderCircle,Pause,Play,RefreshCw,Save,Upload}from'lucide-react';
import './amazonVoc.css';
import{amazonVocEndpoints as ENDPOINTS,buildAmazonVocRunForm,hasAmazonVocArtifacts,isAmazonVocActive}from'./amazonVocState';

const STATUS={uploaded:'已上传待运行',cleaned:'评论已清洗',extracting:'提取评论事件',reviewing:'复核事件证据',normalizing:'规范洞察标签',segmenting:'整理需求分群',journey:'构建用户旅程',compiling:'生成 HTML / Excel',completed:'已完成',failed:'失败',cancelled:'已取消'};
const MODULES=['评论事件','需求标签','评论对比','需求分群','用户旅程','探索性信号'];
const providersOf=settings=>settings?.providers||[];
const modelsOf=settings=>providersOf(settings).flatMap(provider=>(provider.model_refs||[]).map(ref=>({ref,provider:provider.name})));
const credentialsOf=(settings,model)=>providersOf(settings).find(p=>p.name===String(model||'').split('/')[0])?.credentials?.filter(c=>c.id&&c.enabled!==false&&c.authorized!==false)||[];
const defaultCredential=(settings,model)=>providersOf(settings).find(p=>p.name===String(model||'').split('/')[0])?.credentials?.find(c=>c.id&&c.enabled!==false&&c.authorized!==false)?.id||'';
const dateText=value=>{if(!value)return'—';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString()};

export function AmazonVOC({api,initialRunId='',initialNotice='',onNoticeConsumed}){
 const[settings,setSettings]=useState(null),[projects,setProjects]=useState([]),[projectId,setProjectId]=useState('');
 const[projectName,setProjectName]=useState(''),[marketplace,setMarketplace]=useState('US'),[country,setCountry]=useState('US'),[language,setLanguage]=useState('en'),[category,setCategory]=useState('');
 const[model,setModel]=useState(''),[credentialId,setCredentialId]=useState(''),[file,setFile]=useState(null),[runs,setRuns]=useState([]),[runId,setRunId]=useState('');
 const[events,setEvents]=useState([]),[eventIndex,setEventIndex]=useState(0),[eventJson,setEventJson]=useState(''),[note,setNote]=useState(''),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const runRef=useRef('');
 const modelOptions=useMemo(()=>modelsOf(settings),[settings]);
 const credentialOptions=useMemo(()=>credentialsOf(settings,model),[settings,model]);
 const selected=runs.find(item=>item.id===runId)||null;
 const canEdit=selected?.status==='completed'&&events.length>0;
 const reportReady=hasAmazonVocArtifacts(selected);

 const refreshProjects=useCallback(async()=>{
  const value=await api(ENDPOINTS.projects(),undefined,'GET');setProjects(Array.isArray(value)?value:[]);
  setProjectId(current=>current&&value.some(item=>item.id===current)?current:value[0]?.id||'');
 },[api]);
 const refreshRuns=useCallback(async(selectedProject=projectId)=>{
  const value=await api(ENDPOINTS.runs(selectedProject),undefined,'GET');const list=Array.isArray(value)?value:[];setRuns(list);
  const current=runRef.current||runId;const next=current&&list.some(item=>item.id===current)?current:list[0]?.id||'';
  runRef.current=next;setRunId(next);return list;
 },[api,projectId,runId]);

 useEffect(()=>{let alive=true;(async()=>{setLoading(true);try{
  const[config,projectList]=await Promise.all([api(ENDPOINTS.settings(),undefined,'GET'),api(ENDPOINTS.projects(),undefined,'GET')]);
  let linkedRun=null;
  if(initialRunId){try{linkedRun=await api(ENDPOINTS.run(initialRunId),undefined,'GET')}catch(e){if(alive)setError(`无法打开 VOC 任务 ${initialRunId}：${e.message}`)}}
  if(!alive)return;setSettings(config);setProjects(projectList||[]);
  if(linkedRun){setRuns([linkedRun]);runRef.current=linkedRun.id;setRunId(linkedRun.id);setProjectId(linkedRun.project_id||projectList?.find(item=>item.name===linkedRun.project_name)?.id||'')}
  else setProjectId(projectList?.[0]?.id||'');
  const initial=config?.default_model||modelsOf(config)[0]?.ref||'';setModel(initial);setCredentialId(config?.default_credential_id||defaultCredential(config,initial));
  }catch(e){if(alive)setError(e.message)}finally{if(alive)setLoading(false)}})();return()=>{alive=false}},[api,initialRunId]);
 useEffect(()=>{refreshRuns(projectId).catch(e=>setError(e.message))},[projectId,refreshRuns]);
 useEffect(()=>{if(!initialNotice)return;setNotice(initialNotice);onNoticeConsumed?.('')},[initialNotice,onNoticeConsumed]);
 useEffect(()=>{if(!runId)return;let alive=true;const poll=async()=>{try{
  const fresh=await api(ENDPOINTS.run(runId),undefined,'GET');if(!alive)return;
  setRuns(current=>current.map(item=>item.id===fresh.id?fresh:item));
 }catch(e){if(alive)setError(e.message)}};poll();const timer=setInterval(poll,2200);return()=>{alive=false;clearInterval(timer)}},[api,runId]);
 useEffect(()=>{runRef.current=runId},[runId]);

 const createProject=async()=>{
  if(!projectName.trim()||!category.trim())return;
  setBusy(true);setError('');try{const created=await api(ENDPOINTS.projects(),{name:projectName.trim(),marketplace,country,language,category:category.trim()});
   await refreshProjects();setProjectId(created.id);setProjectName('');setNotice('项目已创建，可上传评论 Excel 开始分析。');
  }catch(e){setError(e.message)}finally{setBusy(false)}
 };
 const start=async()=>{
  if(!projectId||!file||busy)return;setBusy(true);setError('');setNotice('');
  try{const body=buildAmazonVocRunForm({file,model,credentialId});const response=await fetch(ENDPOINTS.projectRuns(projectId),{method:'POST',body});
   const value=await response.json().catch(()=>null);if(!response.ok)throw new Error(value?.detail?.message||value?.detail||`任务提交失败（HTTP ${response.status}）`);
   setRuns(current=>[value,...current.filter(item=>item.id!==value.id)]);runRef.current=value.id;setRunId(value.id);setFile(null);setNotice('评论分析任务已提交；工作台会自动运行并刷新结果。');
  }catch(e){setError(e.message)}finally{setBusy(false)}
 };
 const act=async action=>{if(!selected)return;setBusy(true);setError('');try{const value=await api(ENDPOINTS[action](selected.id),{},'POST');setRuns(current=>current.map(item=>item.id===value.id?value:item));setNotice(action==='cancel'?'已请求取消任务。':'已重新排队。')}catch(e){setError(e.message)}finally{setBusy(false)}};
 const loadEvents=async()=>{if(!selected)return;setBusy(true);setError('');try{const doc=await api(ENDPOINTS.events(selected.id),undefined,'GET');const rows=doc.events||[];setEvents(rows);setEventIndex(0);setEventJson(rows.length?JSON.stringify(rows[0],null,2):'');}catch(e){setError(e.message)}finally{setBusy(false)}};
 const changeEventIndex=index=>{setEventIndex(index);setEventJson(JSON.stringify(events[index]||{},null,2))};
 const saveEvent=async()=>{if(!selected||!events[eventIndex])return;setBusy(true);setError('');try{
  const changed=JSON.parse(eventJson);const result=await api(ENDPOINTS.correctEvent(selected.id,events[eventIndex].event_id),{event:changed,note:note||'工作台人工修订'},'PATCH');
  setEvents([]);setNotice(`修订已保存为 revision ${result.revision}，工作台将自动重跑并更新报告。`);await refreshRuns(projectId);
 }catch(e){setError(e instanceof SyntaxError?'事件 JSON 格式无效，请检查后重试。':e.message)}finally{setBusy(false)}
 };
 const canRetry=selected&&['failed','cancelled'].includes(selected.status);
 const stage=selected?STATUS[selected.status]||selected.status:'选择或创建一个项目';

 return <main className="amazon-voc-page">
  <header className="av-heading"><div><div className="av-eyebrow">GEWU · REVIEW INSIGHTS</div><h1>Amazon VOC <span>评论洞察分析</span></h1><p>单个评论 Excel 驱动的证据分析。输出评论事件、需求分群、评论对比、用户旅程及探索性信号。</p></div><div className={'av-model-state '+(loading?'loading':settings?.configured?'ready':'missing')}><i/><div><b>{loading?'正在读取总设置':settings?.configured?'共用工作台文本模型':'尚未配置文本模型'}</b><small>{modelOptions.length?`${modelOptions.length} 个已配置模型`:'请在总设置中添加并验证模型'}</small></div></div></header>
  {error&&<div className="av-banner error" role="alert"><CircleAlert size={16}/><span>{error}</span><button onClick={()=>setError('')} aria-label="关闭错误">×</button></div>}{notice&&<div className="av-banner notice"><CheckCircle2 size={16}/>{notice}</div>}
  <div className="av-layout">
   <section className="av-card av-setup"><div className="av-card-title"><div><span className="av-step">01</span><h2>项目与输入</h2></div><small>分析数据只有评论 Excel</small></div>
    <label className="av-field">选择项目<select value={projectId} onChange={e=>setProjectId(e.target.value)}><option value="">选择 VOC 项目</option>{projects.map(item=><option key={item.id} value={item.id}>{item.name} · {item.marketplace} · {item.category}</option>)}</select></label>
    <details className="av-create-project"><summary>新建 VOC 项目</summary><div className="av-fields"><label>项目名<input value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="例如：Tankini 评论分析"/></label><label>品类<input value={category} onChange={e=>setCategory(e.target.value)} placeholder="例如：Swimwear"/></label><label>Amazon 站点<input value={marketplace} onChange={e=>setMarketplace(e.target.value.toUpperCase())} maxLength={16}/></label><label>国家/地区<input value={country} onChange={e=>setCountry(e.target.value)}/></label><label>评论语言<input value={language} onChange={e=>setLanguage(e.target.value)}/></label><button disabled={busy||!projectName.trim()||!category.trim()} onClick={createProject}>创建项目</button></div></details>
    <label className="av-field">评论 Excel（唯一分析输入）<input type="file" accept=".xlsx,.xlsm" onChange={e=>{const candidate=e.target.files?.[0]||null;setError('');if(candidate&&!/\.(xlsx|xlsm)$/i.test(candidate.name)){setFile(null);setError('仅支持 .xlsx 或 .xlsm 评论 Excel。')}else setFile(candidate)}}/><small>{file?`${file.name} · ${(file.size/1024/1024).toFixed(2)} MiB`:'支持工作台导出表及常见评论表头'}</small></label>
    <div className="av-model-fields"><label>共享文本模型<select value={model} onChange={e=>{setModel(e.target.value);setCredentialId(defaultCredential(settings,e.target.value))}}><option value="">选择模型</option>{modelOptions.map(item=><option key={item.ref} value={item.ref}>{item.ref}</option>)}</select></label><label>使用的凭据<select value={credentialId} onChange={e=>setCredentialId(e.target.value)}><option value="">选择已授权凭据</option>{credentialOptions.map(item=><option key={item.id} value={item.id}>{item.name||item.id}</option>)}</select></label></div>
    <div className="av-callout"><BookOpenCheck size={17}/><span>仅上传这一份评论表。站点、国家、语言和品类用于解释评论上下文，不会引入 Alexa、外部补全或其他分析数据源。文本模型与凭据沿用工作台“总设置”。</span></div>
    <button className="av-primary" disabled={busy||!projectId||!file||!model||!credentialId} onClick={start}>{busy?<LoaderCircle className="av-spin" size={16}/>:<Upload size={16}/>}上传并开始分析</button>
   </section>
   <section className="av-card av-progress"><div className="av-card-title"><div><span className="av-step">02</span><h2>运行与交付</h2></div><button className="av-icon-button" aria-label="刷新任务" onClick={()=>refreshRuns(projectId).catch(e=>setError(e.message))}><RefreshCw size={15}/></button></div>
    <label className="av-field">项目任务<select value={runId} onChange={e=>{runRef.current=e.target.value;setRunId(e.target.value);setEvents([])}}><option value="">选择任务</option>{runs.map(item=><option key={item.id} value={item.id}>{item.id} · {STATUS[item.status]||item.status} · rev {item.current_revision}</option>)}</select></label>
    {selected?<><div className="av-run-summary"><div className="av-run-line"><span className={'av-status '+selected.status}>{stage}</span><b>revision {selected.current_revision}</b><small>{dateText(selected.created_at)}</small></div><div className="av-run-meta"><span>{selected.project_name}</span><span>{selected.marketplace} · {selected.category}</span><span>{selected.sources?.reviews}</span><span>{selected.model_ref}</span></div>{selected.error&&<div className="av-run-error"><CircleAlert size={15}/>{selected.error}</div>}</div>
     {reportReady&&<div className="av-delivery"><b>分析结果已生成</b><small>包含六个评论洞察模块；Excel 证据库含 12 个工作表。</small><div className="av-downloads"><a href={ENDPOINTS.artifact(selected.id,'html')} target="_blank" rel="noreferrer"><ArrowDownToLine size={15}/>打开 HTML 报告</a><a href={ENDPOINTS.artifact(selected.id,'excel')}><ArrowDownToLine size={15}/>下载 Excel 证据库</a><a href={ENDPOINTS.artifact(selected.id,'quality')} target="_blank" rel="noreferrer">质量报告</a></div><div className="av-modules">{MODULES.map((item,index)=><span key={item}><i>{index+1}</i>{item}</span>)}</div></div>}
     <div className="av-run-actions">{isAmazonVocActive(selected.status)&&<button onClick={()=>act('cancel')} disabled={busy}><Pause size={14}/>取消</button>}{canRetry&&<button onClick={()=>act('retry')} disabled={busy}><RefreshCw size={14}/>重试</button>}{selected.status==='completed'&&<button onClick={loadEvents} disabled={busy}><BookOpenCheck size={14}/>检查/修订评论事件</button>}</div>
     {canEdit&&<div className="av-event-editor"><div className="av-editor-heading"><b>人工检查与修订</b><small>保存修订会生成新 revision 并自动重跑。</small></div><label>评论事件<select value={eventIndex} onChange={e=>changeEventIndex(Number(e.target.value))}>{events.map((event,index)=><option key={event.event_id||index} value={index}>{event.event_id||`事件 ${index+1}`} · {event.evidence_quote||''}</option>)}</select></label><textarea value={eventJson} onChange={e=>setEventJson(e.target.value)} placeholder="先点击“检查/修订评论事件”加载事件。"/><input value={note} onChange={e=>setNote(e.target.value)} placeholder="修订说明（可选）"/><button onClick={saveEvent} disabled={busy||!events.length}><Save size={14}/>保存修订并重跑</button></div>}
    </>:<div className="av-empty"><Play size={20}/><b>选择或创建项目并上传评论表</b><span>任务提交后会自动分析，完成时可查看 HTML 和 Excel 结果。</span></div>}
    {runs.length>0&&<div className="av-history"><b>任务历史</b>{runs.slice(0,8).map(item=><button key={item.id} className={item.id===runId?'selected':''} onClick={()=>{runRef.current=item.id;setRunId(item.id);setEvents([])}}><span>{STATUS[item.status]||item.status}</span><small>{item.id} · rev {item.current_revision}</small><time>{dateText(item.created_at)}</time></button>)}</div>}
   </section>
  </div>
 </main>;
}
