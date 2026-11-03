import React,{useCallback,useEffect,useMemo,useRef,useState}from'react';
import{ArrowDownToLine,ArrowRight,BookOpenCheck,CheckCircle2,CircleAlert,LoaderCircle,Pause,Play,RefreshCw}from'lucide-react';
import{amazonReviewsWorkflowCopy,amazonVocRunHash,buildAmazonReviewsVocHandoffRequest,canStartAmazonReviewsVoc,isAmazonReviewsVocEligible,isAmazonReviewsVocPartial,parseAmazonReviewInput}from'./amazonReviewsState';
import{primaryCredentialForModel}from'./modelCredential';

const API='/api/amazon-reviews';
const SITES=[['US','美国'],['GB','英国'],['DE','德国'],['FR','法国'],['IT','意大利'],['ES','西班牙'],['CA','加拿大'],['JP','日本'],['IN','印度'],['MX','墨西哥'],['BR','巴西'],['AU','澳大利亚'],['AE','阿联酋'],['SA','沙特']];
const MODE_LABEL={ranked:'类目榜单',asins:'指定 ASIN',node_asins:'类目 + 指定 ASIN'};
const collectionSourceLabel=source=>source==='auto'?'自动（Woot → Sorftime）':source==='woot'?'仅 Woot':source==='brightdata'?'Bright Data':'Sorftime MCP';
const STATUS={created:'已创建',pending:'待处理',queued:'排队中',running:'执行中',collecting:'拉取评论',translating:'翻译中',cancelling:'正在取消',cancelled:'已取消',completed:'已完成',completed_with_errors:'部分失败',partial:'部分失败',paused:'已暂停',interrupted:'已中断',failed:'失败'};
function providersFrom(settings){
 if(Array.isArray(settings?.providers))return settings.providers;
 if(Array.isArray(settings?.model_settings?.providers))return settings.model_settings.providers;
 if(Array.isArray(settings?.models?.providers))return settings.models.providers;
 return [];
}
function settingsOptions(settings){
 const providers=providersFrom(settings), options=providers.flatMap(provider=>(provider.model_refs||[]).map(ref=>({ref,provider:provider.name}))).filter(x=>x.ref);
 if(options.length)return options;
 const raw=settings?.models||settings?.available_models||[];
 return Array.isArray(raw)?raw.map(item=>typeof item==='string'?{ref:item,provider:item.split('/')[0]}:{ref:item.ref||item.model||item.model_ref,provider:item.provider||item.provider_name||String(item.ref||item.model||'').split('/')[0]}).filter(x=>x.ref):[];
}
function defaultCredential(settings,model){return primaryCredentialForModel(settings,model)}
function jobProgress(job){
 const progress=job?.progress||{};
 const total=Number(progress.total??job?.total??job?.asin_count??job?.items?.length??job?.asins?.length)||0;
 const done=Number(progress.done??progress.completed??job?.completed??job?.processed??job?.completed_count)||0;
 return {done,total,pct:total?Math.min(100,Math.round(done/total*100)):0,label:progress.message||job?.stage_label||job?.stage||''};
}
function jobItems(job){return Array.isArray(job?.items)?job.items:Array.isArray(job?.asins)?job.asins:Array.isArray(job?.products)?job.products:[]}
function itemAsin(item){return item.asin||item.product_id||item.id||'—'}
function itemTitle(item){return item.product_title||item.title||item.name||''}
function itemCount(item){return item.review_count??item.reviews_count??item.count??item.total_reviews??null}
function dateText(value){if(!value)return '—';const date=new Date(typeof value==='number'&&value<10000000000?value*1000:value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString()}
function durationText(value){const n=Number(value);if(!Number.isFinite(n)||n<0)return '—';const s=Math.floor(n);return s<60?`${s} 秒`:`${Math.floor(s/60)} 分 ${s%60} 秒`}

export function AmazonReviews({api,onNavigate,workflowMode='reviews_only',translateEnabled=true,workspaceId='',forceRefresh=false}){
 const[settings,setSettings]=useState(null),[mode,setMode]=useState('ranked'),[site,setSite]=useState('US'),[collectionSource,setCollectionSource]=useState('auto'),[nodeId,setNodeId]=useState(''),[rankLimit,setRankLimit]=useState(50),[asinText,setAsinText]=useState(''),[model,setModel]=useState(''),[credentialId,setCredentialId]=useState('');
 const[jobs,setJobs]=useState([]),[selectedId,setSelectedId]=useState(''),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const[handoffPendingId,setHandoffPendingId]=useState('');
 const selectedRef=useRef(''),handoffPendingRef=useRef('');
 const autoOpenedVocRef=useRef('');
 const modelOptions=useMemo(()=>settingsOptions(settings),[settings]);
 const sorftimeReady=Boolean(settings?.sorftime?.key_configured);
 const brightdataReady=Boolean(settings?.brightdata?.key_configured);
 const effectiveSource=site==='US'?collectionSource:'sorftime';
 const sorftimeNeeded=effectiveSource==='sorftime'||effectiveSource==='auto'||(mode!=='asins'&&effectiveSource!=='brightdata');
 const brightdataNeeded=effectiveSource==='brightdata';
 const brightdataModeUnsupported=brightdataNeeded&&mode!=='asins';

 const refreshJobs=useCallback(async()=>{
  const value=await api(`${API}/jobs`,undefined,'GET');
  const list=Array.isArray(value)?value:Array.isArray(value?.jobs)?value.jobs:Array.isArray(value?.items)?value.items:[];
  setJobs(list);
  const next=selectedRef.current&&list.some(job=>job.id===selectedRef.current)?selectedRef.current:(list[0]?.id||'');
  if(next!==selectedRef.current){selectedRef.current=next;setSelectedId(next)}
  return list;
 },[api]);
 useEffect(()=>{let alive=true;(async()=>{setLoading(true);try{const[value]=await Promise.all([api(`${API}/settings`,undefined,'GET'),refreshJobs()]);if(!alive)return;setSettings(value);const options=settingsOptions(value);const initial=String(value?.default_model||value?.defaults?.[0]||options[0]?.ref||'');setModel(initial);setCredentialId(value?.default_credential_id||defaultCredential(value,initial))}catch(e){if(alive)setError(e.message)}finally{if(alive)setLoading(false)}})();return()=>{alive=false}},[api,refreshJobs]);
 const chooseModel=value=>{setModel(value);setCredentialId(defaultCredential(settings,value))};
 const selectJob=id=>{selectedRef.current=id;setSelectedId(id)};
 const selected=jobs.find(job=>job.id===selectedId)||null;
 useEffect(()=>{
  const runId=selected?.voc_handoff?.run_id;
  if(workflowMode!=='combined'||!runId||autoOpenedVocRef.current===runId)return;
  autoOpenedVocRef.current=runId;
  onNavigate?.(amazonVocRunHash(runId),'评论采集已完成，VOC 已自动启动；如分析失败，可在该 VOC 任务中直接重试。');
 },[workflowMode,selected?.id,selected?.voc_handoff?.run_id,onNavigate]);

 useEffect(()=>{
  if(!selectedId)return undefined;let alive=true;
  const refresh=async()=>{try{const value=await api(`${API}/jobs/${encodeURIComponent(selectedId)}`,undefined,'GET');if(!alive)return;setJobs(items=>{const found=items.some(job=>job.id===value.id);return found?items.map(job=>job.id===value.id?value:job):[value,...items]})}catch(e){if(alive)setError(e.message)}};
  refresh();const timer=setInterval(refresh,2500);return()=>{alive=false;clearInterval(timer)};
 },[api,selectedId]);

 const parsedInput=useMemo(()=>parseAmazonReviewInput(asinText,{allowWootReviewLinks:site==='US'&&(collectionSource==='woot'||collectionSource==='auto')}),[asinText,site,collectionSource]);
 const parsedAsins=parsedInput.asins,invalidAsins=parsedInput.invalid;
 const wootRankedUnsupported=mode==='ranked'&&effectiveSource==='woot';
 const canSubmit=!wootRankedUnsupported&&!brightdataModeUnsupported&&(!sorftimeNeeded||sorftimeReady)&&(!brightdataNeeded||brightdataReady)&&(!translateEnabled||!!model&&!!credentialId)&&!busy&&((mode==='ranked'&&!!nodeId.trim())||(mode==='asins'&&parsedAsins.length>0&&!invalidAsins.length)||(mode==='node_asins'&&!!nodeId.trim()&&parsedAsins.length>0&&!invalidAsins.length));
 const workflowCopy=amazonReviewsWorkflowCopy({translateEnabled,workflowMode});
 const start=async()=>{
  if(!canSubmit)return;setBusy(true);setError('');setNotice('');
  if(effectiveSource==='brightdata'){
   const maxRecords=parsedAsins.length*20;
   const accepted=window.confirm(`本次将调用 Bright Data 采集 ${parsedAsins.length} 个美国站 ASIN，每个最多返回 20 条，最多 ${maxRecords} 条评论。服务商可能按成功返回的评论计费；额度和实际费用以 Bright Data 账户为准。是否继续？`);
   if(!accepted){setBusy(false);return}
  }
  const body={mode,site,model,credential_id:credentialId,workflow_mode:workflowMode,
   translate_enabled:translateEnabled,workspace_id:workspaceId,force_refresh:forceRefresh,
   auto_voc_policy:workflowMode==='combined'?'any_exported':'none'};
   body.collection_source=effectiveSource;
  if(mode==='ranked'){body.node_id=nodeId.trim();body.rank_limit=Number(rankLimit)}
  if(mode==='asins'){body.asins=parsedAsins;if(Object.keys(parsedInput.wootReviewUrls).length)body.woot_review_urls=parsedInput.wootReviewUrls}
  if(mode==='node_asins'){body.node_id=nodeId.trim();body.asins=parsedAsins;if(Object.keys(parsedInput.wootReviewUrls).length)body.woot_review_urls=parsedInput.wootReviewUrls}
  try{const created=await api(`${API}/jobs`,body);setJobs(items=>[created,...items.filter(job=>job.id!==created.id)]);selectJob(created.id);setNotice('评论任务已提交，正在更新进度。')}
  catch(e){setError(e.message)}finally{setBusy(false)}
 };
 const act=async action=>{if(!selected)return;setBusy(true);setError('');setNotice('');try{const updated=await api(`${API}/jobs/${encodeURIComponent(selected.id)}/${action}`,{});setJobs(items=>items.map(job=>job.id===updated.id?updated:job));setNotice(action==='cancel'?'已请求取消任务。':'已提交重试。')}catch(e){setError(e.message)}finally{setBusy(false)}};
 const sendToVoc=async()=>{
  if(!canStartAmazonReviewsVoc(selected,handoffPendingRef.current))return;
  handoffPendingRef.current=selected.id;
  setHandoffPendingId(selected.id);setError('');setNotice('');
  try{
   const request=buildAmazonReviewsVocHandoffRequest(selected.id);
   const value=await api(request.path,request.body);
   if(!value?.run?.id||!value?.source_job)throw new Error('VOC 服务未返回已创建的任务信息，请刷新后检查任务状态。');
   setJobs(items=>items.map(job=>job.id===selected.id?{...job,...value.source_job}:job));
   const message=value.duplicate?'已存在对应的 Amazon VOC 任务，正在打开该任务。':'评论 Excel 已核对并提交，正在打开 Amazon VOC 任务。';
   setNotice(message);
   onNavigate?.(amazonVocRunHash(value.run.id),message);
  }catch(e){setError(e.message)}finally{handoffPendingRef.current='';setHandoffPendingId('')}
 };
 const progress=selected?jobProgress(selected):{done:0,total:0,pct:0,label:''},items=selected?jobItems(selected):[];
 const status=selected?.status||'';
 const canCancel=!!selected&&['queued','running','collecting','translating','created','pending'].includes(status);
 const canRetry=!!selected&&(['paused','interrupted','completed_with_errors','partial','failed','cancelled'].includes(status)||Number(selected.retryable)>0);
 const canDownload=!!selected&&(selected.can_download||selected.download_ready||['completed','completed_with_errors','partial'].includes(status));
 const errors=selected?(Array.isArray(selected.errors)?selected.errors:[]):[];
 return <main className="amazon-reviews-page">
  <header className="ar-heading"><div><div className="ar-eyebrow">GEWU · REVIEW RESEARCH</div><h1>Amazon 评论 <span>{workflowCopy.heading}</span></h1><p>从美国及其他 Amazon 站点获取商品评论，{workflowCopy.introduction}</p></div><div className={'ar-settings-state '+(loading?'loading':settings?'ready':'missing')}><span/><div><b>{loading?'正在读取模型配置':settings?'模型配置已加载':'模型配置不可用'}</b><small>{modelOptions.length?`${modelOptions.length} 个模型可选`:'请在总设置中配置模型与可用 Key'}</small></div></div></header>
  {error&&<div className="ar-banner error" role="alert"><CircleAlert size={16}/><span>{error}</span><button onClick={()=>setError('')} aria-label="关闭错误">×</button></div>}{notice&&<div className="ar-banner notice"><CheckCircle2 size={16}/>{notice}</div>}
  <div className="ar-layout">
   <section className="ar-card ar-form-card"><div className="ar-card-title"><div><span className="ar-step">01</span><h2>创建评论任务</h2></div><small>{workflowCopy.task}</small></div>
    <fieldset className="ar-mode"><legend>输入方式</legend>{Object.entries(MODE_LABEL).map(([value,label])=><label className={mode===value?'selected':''} key={value}><input type="radio" name="ar-mode" value={value} checked={mode===value} onChange={()=>setMode(value)}/><span><b>{label}</b><small>{value==='ranked'?'按类目销量榜单选取商品':value==='asins'?'仅处理指定商品': '先取类目榜单，再限定指定商品'}</small></span></label>)}</fieldset>
    <div className="ar-form-grid"><label>Amazon 站点<select value={site} onChange={e=>setSite(e.target.value)}>{SITES.map(([code,name])=><option key={code} value={code}>{name} ({code})</option>)}</select></label>{mode==='ranked'&&<label>榜单规模<select value={rankLimit} onChange={e=>setRankLimit(Number(e.target.value))}><option value={50}>Top 50</option><option value={100}>Top 100</option></select></label>}</div>
    {site==='US'&&<label className="ar-input-label ar-source-select">评论采集方式<select value={collectionSource} onChange={e=>setCollectionSource(e.target.value)}><option value="auto">自动：Woot 优先，必要时 Sorftime 兜底</option><option value="sorftime">仅 Sorftime MCP（每个 ASIN 最多 100 条，最近一年）</option><option value="woot">仅 Woot 爬虫（需精确商品匹配，不自动切换）</option><option value="brightdata">Bright Data（需总设置 Key；每个 ASIN 最多 20 条）</option></select><small>自动模式需配置 Sorftime。Bright Data 是独立付费来源，不会自动回退调用；需直接 ASIN 模式并在总设置配置 Key。</small></label>}
    {mode!=='asins'&&<label className="ar-input-label">类目 Node ID<input value={nodeId} onChange={e=>setNodeId(e.target.value)} placeholder="输入 Sorftime 类目节点 ID" spellCheck="false"/></label>}
    {mode!=='ranked'&&<label className="ar-input-label">{effectiveSource==='woot'||effectiveSource==='auto'?'ASIN 或 Woot 评论链接':'ASIN 列表'}<textarea value={asinText} onChange={e=>setAsinText(e.target.value)} placeholder={effectiveSource==='woot'||effectiveSource==='auto'?'每行一个 ASIN 或 Woot 评论链接\nB0XXXXXXXX':'每行一个，也支持逗号分隔\nB0XXXXXXXX'} spellCheck="false"/><small>已识别 {parsedAsins.length} 个唯一 ASIN{invalidAsins.length?` · ${invalidAsins.length} 个输入无法识别`:''}</small>{invalidAsins.length>0&&<span className="ar-inline-error">请修正：{invalidAsins.slice(0,4).join('、')}{invalidAsins.length>4?'…':''}</span>}</label>}
    {translateEnabled&&<div className="ar-model-settings"><div className="ar-model-grid"><label>翻译模型<select value={model} onChange={e=>chooseModel(e.target.value)} disabled={!modelOptions.length}><option value="">选择模型</option>{modelOptions.map(item=><option key={item.ref} value={item.ref}>{item.ref}</option>)}</select></label></div>
    {!loading&&!modelOptions.length&&<p className="ar-help">没有可用模型配置。请先在“总设置”配置模型和授权 Key，再返回此处。</p>}</div>}
    <div className="ar-callout"><BookOpenCheck size={16}/><span>{wootRankedUnsupported?'Woot 需要 ASIN 对应的商品链接；榜单模式没有粘贴商品链接的位置，请切换到 Sorftime MCP。':brightdataModeUnsupported?'Bright Data 目前仅支持美国站“指定 ASIN”模式；请切换输入方式，或选择 Sorftime。':effectiveSource==='brightdata'?'Bright Data 会按美国站商品链接采集，每个 ASIN 最多 20 条；选中后创建任务前还会再次确认。结果仍可能受商品无评论、服务商可见范围和账户状态影响。':effectiveSource==='auto'?(mode==='asins'?'自动模式先在 Woot 公开目录核对精确 ASIN；目录无匹配、搜索达到 200 个候选页上限或读取不完整时改用 Sorftime。Woot 每个 ASIN 可能产生 20 次以上请求；Sorftime 每次最多返回最近一年内 100 条，使用两次 Both 调用且不分页。':'当前榜单/类目输入由 Sorftime 处理；每个 ASIN 最多返回最近一年内 100 条，两次 Both 调用不构成分页。'):effectiveSource==='woot'?'仅 Woot：按 5 个星级 × 4 种排序组合抓取并去重；每个 ASIN 可能产生 20 次以上请求。没有精确商品匹配或接口异常时不会自动改用其他来源。':'Sorftime 每次最多返回最近一年内的 100 条评论；每个 ASIN 使用 Both 参数调用两次并按评论日期、标题和正文去重，这两次调用不构成分页。'}{translateEnabled?'模块会逐条翻译有内容的评论标题和正文。':'本次跳过翻译，仅保留评论原文。'}</span></div>
    {!loading&&sorftimeNeeded&&!sorftimeReady&&<p className="ar-help">请先在“寻龙诀”页面配置 Sorftime MCP，然后返回此页。连接状态只显示是否已配置，不会展示 Key。</p>}
    {!loading&&brightdataNeeded&&!brightdataReady&&<p className="ar-help">请先在“总设置”配置 Bright Data API Key。连接状态只显示是否已配置，不会展示 Key。</p>}
    <div className="ar-actions"><button className="ar-primary" disabled={!canSubmit} onClick={start}>{busy?<LoaderCircle className="ar-spin" size={15}/>:<Play size={15}/>}创建并运行</button></div>
   </section>
   <section className="ar-card ar-job-card"><div className="ar-card-title"><div><span className="ar-step">02</span><h2>任务进度</h2></div><button className="ar-icon-button" onClick={()=>refreshJobs().catch(e=>setError(e.message))} aria-label="刷新任务列表"><RefreshCw size={15}/></button></div>
    {selected?<><div className="ar-job-summary"><div className="ar-job-line"><span className={'ar-status '+status}>{STATUS[status]||status||'未知'}</span><b>{progress.done}{progress.total?` / ${progress.total}`:''}{progress.total?' 个商品已处理':''}</b><small>创建 {dateText(selected.created_at||selected.created_at_utc)}{selected.finished_at?` · 完成 ${dateText(selected.finished_at)} · 执行 ${durationText(selected.total_duration_seconds??selected.duration_seconds)}`:''}</small></div>{progress.total>0&&<div className="ar-progress"><i style={{width:`${progress.pct}%`}}/></div>}<div className="ar-job-meta"><span>{MODE_LABEL[selected.mode]||selected.mode||MODE_LABEL[mode]}</span><span>{selected.site||selected.amz_site||site}</span><span>{collectionSourceLabel(selected.collection_source)}</span>{selected.collection_source==='auto'&&selected.collection_sources&&<span>Woot {selected.collection_sources.woot_asins||0} 个 · Sorftime {selected.collection_sources.sorftime_asins||0} 个（自动回退 {selected.collection_sources.auto_fallback_asins||0} 个）</span>}{progress.label&&<span>{progress.label}</span>}{selected.review_count!=null&&<span>{selected.review_count} 条评论</span>}<span>{selected.metrics?.review_tool_calls||0} 次评论接口请求 · 缓存命中 {selected.metrics?.cached_review_requests||0} 次</span></div></div>
     {selected.usage_ledger&&<details className="ar-usage-ledger"><summary>费用与请求账本 · 实际费用未知</summary><pre>{JSON.stringify(selected.usage_ledger,null,2)}</pre></details>}
     {selected.error&&<div className="ar-job-error"><CircleAlert size={15}/>{typeof selected.error==='string'?selected.error:JSON.stringify(selected.error)}</div>}{errors.length>0&&<details className="ar-errors"><summary>失败详情 · {errors.length}</summary>{errors.map((item,index)=><p key={index}>{typeof item==='string'?item:JSON.stringify(item)}</p>)}</details>}
      <div className="ar-job-actions">{canCancel&&<button onClick={()=>act('cancel')} disabled={busy}><Pause size={14}/>取消任务</button>}{canRetry&&<button onClick={()=>act('retry')} disabled={busy}><RefreshCw size={14}/>重试失败项</button>}{canDownload&&<a href={`${API}/jobs/${encodeURIComponent(selected.id)}/download`}><ArrowDownToLine size={14}/>下载结果</a>}{(workflowMode!=='combined'||selected.voc_handoff?.status==='failed')&&isAmazonReviewsVocEligible(selected)&&<button className="ar-primary" onClick={sendToVoc} disabled={busy||handoffPendingId===selected.id}>{handoffPendingId===selected.id?<LoaderCircle className="ar-spin" size={14}/>:<ArrowRight size={14}/>}{selected.voc_handoff?.status==='failed'?'重试启动 VOC':'我已核对 Excel，确认并启动 VOC'}</button>}</div>
      {canDownload&&isAmazonReviewsVocPartial(selected)&&!selected.voc_handoff&&Number(selected.review_count)>0&&<p className="ar-help ar-voc-partial-warning" role="note">该评论任务有失败 ASIN；送入 VOC 时只分析已成功导出的评论，不包含失败 ASIN。</p>}
     {items.length>0?<div className="ar-items"><div className="ar-items-heading"><b>商品进度</b><small>{items.length} 个 ASIN</small></div>{items.map((item,index)=><article className="ar-item" key={itemAsin(item)+'-'+index}><div><b>{itemAsin(item)}</b><small>{itemTitle(item)||'商品标题未返回'}</small>{item.collection_source&&<small>来源：{collectionSourceLabel(item.collection_source)}</small>}</div><span className={'ar-item-status '+(item.status||'')}>{STATUS[item.status]||item.status||'待处理'}</span><small className="ar-item-count">{itemCount(item)!=null?`${itemCount(item)} 条评论`:''}</small>{item.fallback_reason&&<p>已从 Woot 自动切换到 Sorftime MCP：{item.fallback_reason}</p>}{(item.error||item.error_message)&&<p>{item.error||item.error_message}</p>}</article>)}</div>:<div className="ar-empty-items"><small>任务商品明细将在后端返回后显示。</small></div>}
    </>:<div className="ar-empty"><span><BookOpenCheck size={22}/></span><b>{loading?'正在读取任务':'暂无评论任务'}</b><p>配置站点、输入方式、模型和 Key 后即可创建任务。已创建任务会显示在此处。</p></div>}
    {jobs.length>1&&<label className="ar-history">最近任务<select value={selectedId} onChange={e=>selectJob(e.target.value)}>{jobs.map(job=><option key={job.id} value={job.id}>{STATUS[job.status]||job.status} · {job.site||job.amz_site||'Amazon'} · {dateText(job.created_at)}</option>)}</select></label>}
   </section>
  </div>
  <footer className="ar-footer">{workflowCopy.footer}</footer>
 </main>
}
