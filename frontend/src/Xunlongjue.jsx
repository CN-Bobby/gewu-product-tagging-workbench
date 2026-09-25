import React,{useEffect,useRef,useState}from'react';
import{Activity,ArrowDownToLine,ArrowUpRight,CircleAlert,Map,Pause,Play,RefreshCw,Settings2}from'lucide-react';

const RUNS='/api/xunlongjue/runs';
const statusNames={queued:'排队中',pending:'等待中',running:'运行中',paused:'已暂停',pausing:'正在暂停',resuming:'正在继续',completed:'已完成',done:'已完成',succeeded:'已完成',all_done:'全部完成',partial_done:'阶段完成，可继续',not_started:'尚未开始',waiting_retry:'等待重试',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
const activeStatuses=new Set(['queued','pending','running','pausing','resuming']);
const runIdOf=run=>String(run?.id??run?.run_id??'');
const runStatus=run=>String(run?.status||'unknown').toLowerCase();
const detailOf=run=>String(run?.stage||run?.current_stage||run?.message||'');
const formatDate=value=>{if(!value)return'—';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString()};
function progressOf(run){
 const raw=run?.progress??run?.progress_percent??run?.percent_complete;
 if(typeof raw==='number')return Math.max(0,Math.min(100,raw<=1?raw*100:raw));
 if(typeof raw==='object'&&raw){const done=Number(raw.done??raw.completed??0),total=Number(raw.total??raw.expected??0);return total>0?Math.max(0,Math.min(100,done/total*100)):null}
 const done=Number(run?.completed_nodes??run?.nodes_completed??NaN),total=Number(run?.total_nodes??run?.nodes_total??NaN);
 return Number.isFinite(done)&&Number.isFinite(total)&&total>0?Math.max(0,Math.min(100,done/total*100)):null;
}
function progressLabel(run){
 const raw=run?.progress;
 if(raw&&typeof raw==='object'){
  const done=raw.done??raw.completed,total=raw.total??raw.expected;
  if(done!=null&&total!=null)return`${done} / ${total}${raw.unit?' '+raw.unit:''}`;
  if(raw.message)return String(raw.message);
 }
 const percent=progressOf(run);return percent==null?'进度暂不可用':`${Math.round(percent)}%`;
}
function errorText(error){return error?.message||'请求失败，请稍后重试。'}

export function Xunlongjue({api}){
 const[settings,setSettings]=useState({endpoint:'',key_configured:false});
 const[endpoint,setEndpoint]=useState('');const[apiKey,setApiKey]=useState('');
 const[settingsLoading,setSettingsLoading]=useState(true),[settingsSaving,setSettingsSaving]=useState(false),[settingsError,setSettingsError]=useState(''),[settingsSaved,setSettingsSaved]=useState(false);
 const[site,setSite]=useState('US'),[nodeId,setNodeId]=useState('');const[maxDepth,setMaxDepth]=useState(''),[maxNodes,setMaxNodes]=useState('');
 const[starting,setStarting]=useState(false),[startError,setStartError]=useState('');
 const[runs,setRuns]=useState([]),[runsLoading,setRunsLoading]=useState(true),[runsError,setRunsError]=useState('');
 const[selectedId,setSelectedId]=useState(''),[detail,setDetail]=useState(null),[detailLoading,setDetailLoading]=useState(false),[detailError,setDetailError]=useState('');
 const[actionBusy,setActionBusy]=useState('');const listRequest=useRef(0),detailRequest=useRef(0);
 const refreshRuns=async({initial=false}={})=>{
  const request=++listRequest.current;
  if(initial)setRunsLoading(true);
  try{
   const result=await api(RUNS,undefined,'GET');
   if(request!==listRequest.current)return;
   const rows=Array.isArray(result)?result:Array.isArray(result?.runs)?result.runs:[];
   setRuns(rows);setRunsError('');
   setSelectedId(current=>current&&rows.some(item=>runIdOf(item)===current)?current:runIdOf(rows[0]));
  }catch(error){if(request===listRequest.current)setRunsError(errorText(error))}
  finally{if(request===listRequest.current&&initial)setRunsLoading(false)}
 };
 useEffect(()=>{
  let live=true;
  api('/api/xunlongjue/settings',undefined,'GET').then(value=>{
   if(!live)return;const next={endpoint:String(value?.endpoint||''),key_configured:Boolean(value?.key_configured)};
   setSettings(next);setEndpoint(next.endpoint);
  }).catch(error=>{if(live)setSettingsError(errorText(error))}).finally(()=>{if(live)setSettingsLoading(false)});
  void refreshRuns({initial:true});
  return()=>{live=false;listRequest.current++};
 },[]);
 useEffect(()=>{
  const timer=window.setInterval(()=>{if(runs.some(run=>activeStatuses.has(runStatus(run))))void refreshRuns()},5000);
  return()=>window.clearInterval(timer);
 },[runs]);
 useEffect(()=>{
  if(!selectedId){setDetail(null);setDetailError('');setDetailLoading(false);return undefined}
  let live=true;const request=++detailRequest.current;setDetailLoading(true);setDetailError('');setDetail(null);
  const load=async()=>{
   try{const value=await api(`${RUNS}/${encodeURIComponent(selectedId)}`,undefined,'GET');if(live&&request===detailRequest.current)setDetail(value)}
   catch(error){if(live&&request===detailRequest.current)setDetailError(errorText(error))}
   finally{if(live&&request===detailRequest.current)setDetailLoading(false)}
  };
  void load();
  return()=>{live=false;detailRequest.current++};
 },[selectedId]);
 useEffect(()=>{
  if(!selectedId||!activeStatuses.has(runStatus(detail||runs.find(run=>runIdOf(run)===selectedId))))return undefined;
  let live=true,timer;
  const poll=async()=>{
   try{const value=await api(`${RUNS}/${encodeURIComponent(selectedId)}`,undefined,'GET');if(!live)return;setDetail(value);setDetailError('')}
   catch(error){if(live)setDetailError(errorText(error))}
   finally{if(live)timer=window.setTimeout(poll,3500)}
  };
  timer=window.setTimeout(poll,3500);
  return()=>{live=false;window.clearTimeout(timer)};
 },[selectedId,runStatus(detail||runs.find(run=>runIdOf(run)===selectedId))]);
 const saveSettings=async event=>{
  event.preventDefault();setSettingsSaving(true);setSettingsError('');setSettingsSaved(false);
  try{
   const value=await api('/api/xunlongjue/settings',{endpoint:endpoint.trim(),api_key:apiKey.trim()||null},'PUT');
   const next={endpoint:String(value?.endpoint??endpoint.trim()),key_configured:value?.key_configured==null?(Boolean(apiKey.trim())||settings.key_configured):Boolean(value.key_configured)};
   setSettings(next);setEndpoint(next.endpoint);setApiKey('');setSettingsSaved(true);
  }catch(error){setSettingsError(errorText(error))}finally{setSettingsSaving(false)}
 };
 const startRun=async event=>{
  event.preventDefault();setStartError('');
  if(!site.trim()||!nodeId.trim()){setStartError('请填写站点和类目 Node ID。');return}
  setStarting(true);
  try{
   const body={site:site.trim(),node_id:nodeId.trim()};
   if(maxDepth.trim())body.max_depth=Number(maxDepth);
   if(maxNodes.trim())body.max_nodes=Number(maxNodes);
   const run=await api(RUNS,body);
   const id=runIdOf(run);if(id)setSelectedId(id);
   await refreshRuns();
   if(id)setSelectedId(id);
   setNodeId('');
  }catch(error){setStartError(errorText(error))}finally{setStarting(false)}
 };
 const runAction=async action=>{
  if(!selectedId)return;setActionBusy(action);setDetailError('');
  try{
   const result=await api(`${RUNS}/${encodeURIComponent(selectedId)}/${action}`,{});
   if(result&&typeof result==='object')setDetail(result);
   await refreshRuns();
   const latest=await api(`${RUNS}/${encodeURIComponent(selectedId)}`,undefined,'GET');setDetail(latest);
  }catch(error){setDetailError(errorText(error))}finally{setActionBusy('')}
 };
 const selectedRun=detail||runs.find(run=>runIdOf(run)===selectedId)||null;
 const selectedStatus=runStatus(selectedRun);
 const reportUrl=selectedRun?.report_url||runs.find(run=>runIdOf(run)===selectedId)?.report_url||'';
 const selectedProgress=progressOf(selectedRun);
 const canPause=['queued','pending','running','resuming'].includes(selectedStatus),canResume=['paused','interrupted','partial_done'].includes(selectedStatus);
 return <main className="xj-page">
  <header className="xj-heading"><div><div className="eyebrow">XUNLONGJUE · CATEGORY SCAN</div><h1>寻龙诀</h1><p>配置 Sorftime 服务并发起类目树扫描，跟踪运行进度与成熟报告。</p></div><button className="xj-refresh" onClick={()=>void refreshRuns()} disabled={runsLoading} aria-label="刷新任务列表"><RefreshCw size={15}/>刷新</button></header>
  <section className="xj-grid">
   <div className="xj-column">
    <section className="home-card xj-card"><div className="home-card-head"><div><small>CONNECTION</small><h3><Settings2 size={16}/>Sorftime 服务</h3></div><span className={settings.key_configured?'configured-pill':'missing-pill'}>{settingsLoading?'读取中':settings.key_configured?'Key 已配置':'需要配置 Key'}</span></div>
     <form className="xj-settings" onSubmit={saveSettings}>
      <label className="settings-field"><span className="field-label">API 地址</span><input aria-label="Sorftime API 地址" type="url" value={endpoint} onChange={event=>{setEndpoint(event.target.value);setSettingsSaved(false)}} placeholder="https://api.example.com" required/><small className="field-help">填写 Sorftime MCP 服务地址。</small></label>
      <label className="settings-field"><span className="field-label">API Key</span><input aria-label="Sorftime API Key" type="password" autoComplete="new-password" value={apiKey} onChange={event=>{setApiKey(event.target.value);setSettingsSaved(false)}} placeholder={settings.key_configured?'已配置；留空保留现有 Key':'输入 Sorftime API Key'} autoCapitalize="off" spellCheck="false"/><small className="field-help">密钥不会回显。留空保存地址时按保留现有密钥处理。</small></label>
      {settingsError&&<p className="xj-error" role="alert"><CircleAlert size={15}/>{settingsError}</p>}
      <div className="xj-form-actions"><button className="primary" type="submit" disabled={settingsSaving||settingsLoading}>{settingsSaving?'保存中…':'保存连接设置'}</button>{settingsSaved&&<span className="xj-success">设置已保存</span>}</div>
     </form>
    </section>
    <section className="home-card xj-card"><div className="home-card-head"><div><small>NEW SCAN</small><h3><Map size={16}/>启动类目扫描</h3></div></div>
     <form className="xj-scan-form" onSubmit={startRun}>
      <div className="xj-form-row"><label className="settings-field"><span className="field-label">站点</span><select aria-label="站点" value={site} onChange={event=>setSite(event.target.value)}><option value="US">美国 · US</option><option value="CA">加拿大 · CA</option><option value="UK">英国 · UK</option><option value="DE">德国 · DE</option><option value="FR">法国 · FR</option><option value="IT">意大利 · IT</option><option value="ES">西班牙 · ES</option><option value="JP">日本 · JP</option></select></label><label className="settings-field"><span className="field-label">类目 Node ID</span><input aria-label="类目 Node ID" value={nodeId} onChange={event=>setNodeId(event.target.value)} placeholder="例如 3760901" required/></label></div>
      <details className="xj-advanced"><summary>高级扫描范围</summary><div className="xj-form-row"><label className="settings-field"><span className="field-label">最大深度</span><input aria-label="最大深度" type="number" min="1" step="1" value={maxDepth} onChange={event=>setMaxDepth(event.target.value)} placeholder="按默认范围"/></label><label className="settings-field"><span className="field-label">最大节点数</span><input aria-label="最大节点数" type="number" min="1" step="1" value={maxNodes} onChange={event=>setMaxNodes(event.target.value)} placeholder="按默认范围"/></label></div></details>
      {startError&&<p className="xj-error" role="alert"><CircleAlert size={15}/>{startError}</p>}
      <div className="xj-form-actions"><button className="primary" type="submit" disabled={starting||settingsLoading||!settings.key_configured}>{starting?<><Activity size={15}/>正在启动…</>:<><Play size={15}/>开始扫描</>}</button>{!settingsLoading&&!settings.key_configured&&<span className="xj-muted">先保存 Sorftime 地址和 Key</span>}</div>
     </form>
    </section>
   </div>
   <section className="home-card xj-runs-card"><div className="home-card-head"><div><small>SCAN RUNS</small><h3><Activity size={16}/>运行任务</h3></div><span className="xj-run-count">{runs.length} 项</span></div>
    {runsError&&<div className="xj-error xj-list-error" role="alert"><CircleAlert size={15}/>{runsError}<button onClick={()=>void refreshRuns()}>重试</button></div>}
    {runsLoading&&!runs.length?<div className="xj-empty">正在读取运行记录…</div>:!runs.length?<div className="xj-empty">还没有扫描任务。填写站点与 Node ID 后开始。</div>:<div className="xj-run-layout">
      <div className="xj-run-list" aria-label="扫描任务列表">{runs.map(run=>{const id=runIdOf(run),status=runStatus(run),percent=progressOf(run),nodeId=run.node_id||run.root_node_id;return <button type="button" key={id||run.created_at} className={'xj-run-item '+(id===selectedId?'selected':'')} onClick={()=>id&&setSelectedId(id)}><span className={'xj-status '+status}>{statusNames[status]||status||'未知'}</span><b>{run.title||run.name||run.node_name||`Node ${nodeId||'—'}`}</b><small>{run.site||run.marketplace||'—'} · Node ID {nodeId||'—'}</small><div className="xj-run-item-foot"><small>{detailOf(run)||formatDate(run.created_at||run.started_at)}</small>{percent!=null&&<small>{Math.round(percent)}%</small>}</div></button>})}</div>
     <section className="xj-run-detail" aria-live="polite">
      {detailLoading&&!detail?<div className="xj-empty">正在读取任务详情…</div>:detailError&&!detail?<div className="xj-error" role="alert"><CircleAlert size={15}/>{detailError}</div>:selectedRun?<>
       <div className="xj-detail-title"><div><span className={'xj-status '+selectedStatus}>{statusNames[selectedStatus]||selectedStatus||'未知'}</span><h4>{selectedRun.title||selectedRun.name||selectedRun.node_name||`Node ${selectedRun.node_id||selectedRun.root_node_id||'—'}`}</h4></div><small>{formatDate(selectedRun.created_at||selectedRun.started_at)}</small></div>
       <p className="xj-run-meta">站点 {selectedRun.site||selectedRun.marketplace||site} <span>·</span> Node ID {selectedRun.node_id||selectedRun.root_node_id||'—'}{selectedRun.max_depth!=null&&<> <span>·</span> 深度 {selectedRun.max_depth}</>}</p>
       <div className="xj-progress-head"><b>{detailOf(selectedRun)||'扫描进度'}</b><span>{progressLabel(selectedRun)}</span></div><div className="xj-progress" role="progressbar" aria-label="扫描进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow={selectedProgress==null?undefined:Math.round(selectedProgress)}><span style={{width:selectedProgress==null?(activeStatuses.has(selectedStatus)?'35%':'0%'):`${selectedProgress}%`}} className={selectedProgress==null&&activeStatuses.has(selectedStatus)?'indeterminate':''}/></div>
       {selectedRun.message&&<p className="xj-run-message">{selectedRun.message}</p>}{detailError&&<p className="xj-inline-error" role="status">详情刷新失败：{detailError}</p>}
       <div className="xj-detail-foot"><span>最近更新 {formatDate(selectedRun.updated_at||selectedRun.last_activity||selectedRun.created_at)}</span><div>{canPause&&<button disabled={!!actionBusy} onClick={()=>void runAction('pause')}><Pause size={14}/>{actionBusy==='pause'?'暂停中…':'暂停'}</button>}{canResume&&<button disabled={!!actionBusy} onClick={()=>void runAction('resume')}><Play size={14}/>{actionBusy==='resume'?'继续中…':'继续'}</button>}{reportUrl&&<><button onClick={()=>window.open(reportUrl,'_blank','noopener,noreferrer')}><ArrowUpRight size={14}/>打开报告</button><a className="xj-download" href={reportUrl} download rel="noreferrer"><ArrowDownToLine size={14}/>下载报告</a></>}</div></div>
      </>:<div className="xj-empty">选择一项扫描任务查看进度。</div>}
     </section>
    </div>}
   </section>
  </section>
 </main>;
}
