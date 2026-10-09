import React,{useCallback,useEffect,useMemo,useRef,useState}from'react';
import{ArrowDownToLine,ArrowLeft,ArrowRight,BookOpenCheck,FileSpreadsheet,RefreshCw,Search}from'lucide-react';
import './productArchiveLinkedPages.css';

const REVIEW_STATUS={
 created:'已创建',pending:'待处理',queued:'排队中',running:'执行中',collecting:'拉取评论',translating:'翻译中',
 completed:'已完成',completed_with_errors:'部分失败',partial:'部分失败',failed:'失败',cancelled:'已取消',paused:'已暂停',interrupted:'已中断',
};
const VOC_STATUS={
 uploaded:'已接收',cleaned:'整理中',extracting:'提取洞察',reviewing:'复核证据',normalizing:'整理标签',
 segmenting:'用户分群',journey:'生成旅程',compiling:'汇总报告',completed:'已完成',failed:'失败',cancelled:'已取消',
};
const reviewStatus=status=>REVIEW_STATUS[status]||status||'未知';
const vocStatus=status=>VOC_STATUS[status]||status||'未知';
const dateText=value=>value?new Date(value).toLocaleString('zh-CN'):'—';
const cellText=value=>value==null||value===''?'—':String(value);
const reviewBilingualColumns=headers=>{
 const indexOf=patterns=>headers.findIndex(header=>patterns.some(pattern=>pattern.test(String(header||'').replace(/\s+/g,''))));
 const titleEn=indexOf([/^(?:评论标题|reviewtitle|title).*(?:\(en\)|英文|_en)$/i]);
 const titleZh=indexOf([/^(?:评论标题|reviewtitle|title).*(?:\(中文\)|中文|_zh|_cn)$/i,/^(?:t_title|translated_title|title_zh)$/i]);
 const contentEn=indexOf([/^(?:评论内容|评论正文|reviewcontent|reviewbody|content|body).*(?:\(en\)|英文|_en)$/i]);
 const contentZh=indexOf([/^(?:评论内容|评论正文|reviewcontent|reviewbody|content|body).*(?:\(中文\)|中文|_zh|_cn)$/i,/^(?:t_content|translated_content|translation_zh|content_zh|review_zh)$/i]);
 const pairs=[];
 if(titleEn>=0&&titleZh>=0&&titleEn!==titleZh)pairs.push({key:'review-title',label:'评论标题',enIndex:titleEn,zhIndex:titleZh});
 if(contentEn>=0&&contentZh>=0&&contentEn!==contentZh)pairs.push({key:'review-content',label:'评论内容',enIndex:contentEn,zhIndex:contentZh});
 return pairs;
};
const orderedReviewColumns=(headers,pairs)=>{
 if(!pairs.length)return headers.map((header,index)=>({key:`column-${index}`,label:header,index}));
 const paired=new Set(pairs.flatMap(pair=>[pair.enIndex,pair.zhIndex]));
 const regular=headers.map((header,index)=>({key:`column-${index}`,label:header,index})).filter(column=>!paired.has(column.index));
 const primary=regular.filter(column=>/asin|商品标题|商品名称|评分|星级|评论日期|评论时间|日期/i.test(column.label));
 const primaryIndices=new Set(primary.map(column=>column.index));
 const remaining=regular.filter(column=>!primaryIndices.has(column.index));
 return [...primary,...pairs.map(pair=>({...pair,type:'bilingual'})),...remaining];
};

export function ArchivePageTabs({archiveId='',active='home',onNavigate}){
 const tabs=[
  {id:'home',label:'首页',path:'product-archive'},
  {id:'overview',label:'综合分析',path:`product-archive/${archiveId}`},
  {id:'reviews',label:'评论原文',path:`product-archive/${archiveId}/reviews`},
  {id:'voc',label:'VOC 结果',path:`product-archive/${archiveId}/voc`},
 ];
 return <nav className="pa-page-tabs" aria-label="选品档案分页">{tabs.map(tab=>{
  const disabled=tab.id!=='home'&&!archiveId;
  return <button key={tab.id} type="button" className={active===tab.id?'active':''} aria-current={active===tab.id?'page':undefined}
   disabled={disabled} title={disabled?'先选择或创建一个档案':''} onClick={()=>!disabled&&onNavigate?.(tab.path)}>{tab.label}</button>;
 })}</nav>;
}

export function ReviewOriginalsPage({api,archiveId,archive,onNavigate}){
 const[jobs,setJobs]=useState([]),[jobId,setJobId]=useState(''),[sheet,setSheet]=useState(''),[payload,setPayload]=useState(null);
 const[searchText,setSearchText]=useState(''),[query,setQuery]=useState(''),[offset,setOffset]=useState(0),[loading,setLoading]=useState(true),[error,setError]=useState('');
 const requestId=useRef(0),selectedRef=useRef('');
 const refresh=useCallback(async()=>{
  const value=await api(`/api/product-archive/archives/${encodeURIComponent(archiveId)}/review-jobs`,undefined,'GET');
  const list=Array.isArray(value)?value:[];setJobs(list);
  const current=selectedRef.current;
  const next=current&&list.some(item=>item.id===current)?current:(list.find(item=>item.can_download)?.id||list[0]?.id||'');
  if(next!==current){selectedRef.current=next;setJobId(next);setSheet('');setOffset(0)}
  return list;
 },[api,archiveId]);

 useEffect(()=>{let alive=true;setLoading(true);refresh().catch(e=>alive&&setError(e.message)).finally(()=>alive&&setLoading(false));
  const timer=setInterval(()=>refresh().catch(e=>alive&&setError(e.message)),7000);
  return()=>{alive=false;clearInterval(timer)};
 },[refresh]);
 useEffect(()=>{const timer=setTimeout(()=>{setQuery(searchText.trim());setOffset(0)},250);return()=>clearTimeout(timer)},[searchText]);

 const selected=jobs.find(item=>item.id===jobId)||null;
 const selectedSheet=sheet||(selected?.sheets?.[0]||'');
 useEffect(()=>{
  if(!selected?.can_download||!selectedSheet){setPayload(null);return undefined}
  let alive=true;const current=++requestId.current;setLoading(true);setError('');
  const params=new URLSearchParams({job_id:selected.id,sheet:selectedSheet,offset:String(offset),limit:'50',query});
  api(`/api/product-archive/archives/${encodeURIComponent(archiveId)}/reviews?${params}`,undefined,'GET')
   .then(value=>{if(alive&&current===requestId.current){setPayload(value);setSheet(value.sheet||selectedSheet)}})
   .catch(e=>alive&&current===requestId.current&&setError(e.message))
   .finally(()=>alive&&current===requestId.current&&setLoading(false));
  return()=>{alive=false};
 },[api,archiveId,selected?.id,selected?.can_download,selectedSheet,offset,query]);

  const headers=payload?.headers||[],rows=payload?.rows||[],total=Number(payload?.total)||0;
  const bilingualPairs=useMemo(()=>reviewBilingualColumns(headers),[headers]);
  const tableColumns=useMemo(()=>orderedReviewColumns(headers,bilingualPairs),[headers,bilingualPairs]);
  const hasTranslationHeader=headers.some(header=>/(中文|译文|translation|translated|_zh|_cn|chinese)/i.test(String(header||'')));
 const chooseJob=value=>{selectedRef.current=value;setJobId(value);setSheet('');setOffset(0);setPayload(null)};
 const pageLabel=total?`${offset+1}–${Math.min(offset+rows.length,total)} / ${total}`:'0 条';
 return <main className="pa-page pa-linked-page">
  <ArchivePageTabs archiveId={archiveId} active="reviews" onNavigate={onNavigate}/>
  <button className="pa-back" onClick={()=>onNavigate?.(`product-archive/${archiveId}`)}><ArrowLeft size={14}/>返回综合分析</button>
  <header className="pa-heading"><div><div className="eyebrow">PRODUCT ARCHIVE · REVIEWS</div><h1>评论原文</h1>
   <p>{archive?.category||archive?.title||'当前选品档案'} · 查看关联任务输出或项目归档的评论 Excel 原文。</p></div>
   <span className="pa-phase">只读表格 · 按项目文件与任务关联</span></header>
  <section className="home-card pa-review-browser">
   <div className="pa-linked-toolbar">
    <label>评论任务<select aria-label="评论任务" value={jobId} onChange={event=>chooseJob(event.target.value)}>
     {!jobs.length&&<option value="">暂无关联评论文件</option>}{jobs.map(item=><option value={item.id} key={item.id}>{item.source_kind==='project_artifact'?`项目文件 · ${item.name||'评论 Excel'}`:`${reviewStatus(item.status)} · ${item.site||'Amazon'} · ${dateText(item.created_at)}`}</option>)}
    </select></label>
    {selected?.can_download&&selected?.sheets?.length>0&&<label>工作表<select aria-label="评论 Excel 工作表" value={selectedSheet} onChange={event=>{setSheet(event.target.value);setOffset(0)}}>{(selected.sheets||[]).map(name=><option value={name} key={name}>{name}</option>)}</select></label>}
    <label className="pa-linked-search"><Search size={15}/><input aria-label="搜索评论内容" value={searchText} onChange={event=>setSearchText(event.target.value)} placeholder="搜索 ASIN、标题、评论内容"/></label>
    <button className="pa-linked-refresh" type="button" onClick={()=>refresh().catch(e=>setError(e.message))}><RefreshCw size={14}/>刷新</button>
   </div>
   {selected&&<div className="pa-review-job-summary"><span><b>{selected.source_kind==='project_artifact'?'已归档':reviewStatus(selected.status)}</b><small>{selected.source_kind==='project_artifact'?'文件来源':'任务状态'}</small></span>
    <span><b>{selected.review_count==null?'—':Number(selected.review_count).toLocaleString('zh-CN')}</b><small>评论数</small></span>
    <span><b>{selected.total==null?'—':Number(selected.total).toLocaleString('zh-CN')}</b><small>任务 ASIN</small></span>
    <span><b>{selected.completed==null?'—':`${Number(selected.completed).toLocaleString('zh-CN')} / ${Number(selected.failed||0).toLocaleString('zh-CN')}`}</b><small>成功 / 失败 ASIN</small></span>
    {selected.can_download&&<a href={selected.source_kind==='project_artifact'?`/api/projects/${encodeURIComponent(selected.workspace_id)}/artifacts/${encodeURIComponent(selected.artifact_id)}/download`:`/api/amazon-reviews/jobs/${encodeURIComponent(selected.id)}/download`}><ArrowDownToLine size={14}/>下载原始 Excel</a>}
   </div>}
   {error&&<div className="pa-linked-error" role="alert">{error}</div>}
   {!loading&&!jobs.length?<div className="pa-linked-empty"><BookOpenCheck size={25}/><b>这个档案还没有关联评论文件</b><p>评论任务生成的 Excel 和已登记到项目的评论文件都会显示在这里。</p></div>:
    selected&&(!selected.can_download||!selected.sheets?.length)?<div className="pa-linked-empty"><BookOpenCheck size={25}/><b>{selected.can_download?'Excel 无法读取工作表':`${reviewStatus(selected.status)}，Excel 尚未生成`}</b><p>{selected.can_download?'可以先下载原始 Excel 检查文件。':`评论任务完成并生成结果文件后，表格会出现在这里。`}</p></div>:
     selected&&selected.can_download&&selectedSheet&&<><div className="pa-review-table-head"><b>{selectedSheet||'评论结果'}</b><span>{bilingualPairs.length?'中英对照已并排 · ':''}{loading?'正在读取…':pageLabel}</span></div>
      {payload&&headers.length>0&&!bilingualPairs.length&&<div className="pa-review-language-note" role="status">{hasTranslationHeader?'检测到疑似翻译字段，但没有识别到可配对的中英评论列；下方按 Excel 原表显示。':'当前选择的 Excel 没有可识别的中文翻译列；下方按 Excel 原表显示。'}</div>}
      <div className="pa-linked-table-wrap"><table className="pa-review-table"><thead><tr><th>Excel 行</th>{tableColumns.map(column=><th key={column.key}>{column.type==='bilingual'?<>{column.label}<small className="pa-review-pair-header"><span>EN 原文</span><span>中文译文</span></small></>:column.label}</th>)}</tr></thead>
       <tbody>{rows.map(row=><tr key={row.excel_row}><td>{row.excel_row}</td>{tableColumns.map(column=>column.type==='bilingual'?<td className="pa-review-bilingual" key={`${row.excel_row}-${column.key}`}><div className="pa-review-bilingual-cell"><div><small>EN 原文</small><span>{cellText(row.values?.[column.enIndex])}</span></div><div><small>中文译文</small><span>{cellText(row.values?.[column.zhIndex])}</span></div></div></td>:<td key={`${row.excel_row}-${column.index}`} title={cellText(row.values?.[column.index])}>{cellText(row.values?.[column.index])}</td>)}</tr>)}
        {!loading&&!rows.length&&<tr><td colSpan={tableColumns.length+1} className="pa-review-empty-row">{query?'没有匹配的评论':'这个工作表没有可显示的数据'}</td></tr>}
      </tbody></table></div>
     <div className="pa-linked-pagination"><span>{pageLabel} · 每页 50 条</span><div><button type="button" disabled={offset<=0||loading} onClick={()=>setOffset(Math.max(0,offset-50))}><ArrowLeft size={14}/>上一页</button><button type="button" disabled={loading||offset+rows.length>=total} onClick={()=>setOffset(offset+50)}>下一页<ArrowRight size={14}/></button></div></div>
    </>}
  </section>
 </main>;
}

export function VocResultsPage({api,archiveId,archive,onNavigate}){
 const[runs,setRuns]=useState([]),[runId,setRunId]=useState(''),[loading,setLoading]=useState(true),[error,setError]=useState('');
 const[reportHtml,setReportHtml]=useState(''),[reportError,setReportError]=useState(''),[reportLoading,setReportLoading]=useState(false);
 const selectedRef=useRef('');
 const refresh=useCallback(async()=>{
  const value=await api(`/api/product-archive/archives/${encodeURIComponent(archiveId)}/voc-runs`,undefined,'GET');
  const list=Array.isArray(value)?value:[];setRuns(list);
  const current=selectedRef.current;
  const next=current&&list.some(item=>item.id===current)?current:(list[0]?.id||'');
  if(next!==current){selectedRef.current=next;setRunId(next)}
  return list;
 },[api,archiveId]);
 useEffect(()=>{let alive=true;setLoading(true);refresh().catch(e=>alive&&setError(e.message)).finally(()=>alive&&setLoading(false));
  const timer=setInterval(()=>refresh().catch(e=>alive&&setError(e.message)),5000);
  return()=>{alive=false;clearInterval(timer)};
 },[refresh]);
 const selected=runs.find(item=>item.id===runId)||null;
 const source=selected?.sources?.review_source_context||{};
 const artifacts=selected?.artifacts||[];
 const artifact=(kind)=>`/api/amazon-voc/runs/${encodeURIComponent(selected.id)}/artifacts/${kind}`;
 const reportReady=selected?.status==='completed'&&artifacts.includes('html');
 const reportUrl=reportReady?artifact('html'):'';
 useEffect(()=>{
  if(!reportUrl){setReportHtml('');setReportError('');setReportLoading(false);return}
  const controller=new AbortController();setReportHtml('');setReportError('');setReportLoading(true);
  fetch(reportUrl,{signal:controller.signal}).then(response=>{
   if(!response.ok)throw new Error(`报告加载失败（HTTP ${response.status}）`);
   return response.text();
  }).then(html=>{if(!html.trim())throw new Error('报告内容为空');setReportHtml(html)}).catch(error=>{
   if(error.name!=='AbortError')setReportError(error.message||'报告加载失败');
  }).finally(()=>{if(!controller.signal.aborted)setReportLoading(false)});
  return()=>controller.abort();
 },[reportUrl]);
 const chooseRun=id=>{selectedRef.current=id;setRunId(id)};
 const coverage=useMemo(()=>[
  ['评论行数',source.review_count],['评论 ASIN',source.comment_asin_count],
  ['成功完成 ASIN',source.completed_asin_count],['失败 ASIN',source.failed_asin_count],
 ],[source]);

 return <main className="pa-page pa-linked-page">
  <ArchivePageTabs archiveId={archiveId} active="voc" onNavigate={onNavigate}/>
  <button className="pa-back" onClick={()=>onNavigate?.(`product-archive/${archiveId}`)}><ArrowLeft size={14}/>返回综合分析</button>
  <header className="pa-heading"><div><div className="eyebrow">PRODUCT ARCHIVE · VOICE OF CUSTOMER</div><h1>VOC 结果</h1>
   <p>{archive?.category||archive?.title||'当前选品档案'} · 展示由关联评论任务生成的 VOC 报告。</p></div>
   <span className="pa-phase">只展示已关联任务</span></header>
  <section className="home-card pa-voc-browser">
   <div className="pa-voc-toolbar"><label>VOC 任务<select aria-label="VOC 任务" value={runId} onChange={event=>chooseRun(event.target.value)}>
    {!runs.length&&<option value="">暂无关联 VOC 任务</option>}{runs.map(item=><option value={item.id} key={item.id}>{vocStatus(item.status)} · {dateText(item.created_at)} · rev {item.current_revision||1}</option>)}
   </select></label><button className="pa-linked-refresh" type="button" onClick={()=>refresh().catch(e=>setError(e.message))}><RefreshCw size={14}/>刷新</button></div>
   {error&&<div className="pa-linked-error" role="alert">{error}</div>}
   {!loading&&!runs.length?<div className="pa-linked-empty"><BookOpenCheck size={25}/><b>这个档案还没有关联 VOC 结果</b><p>评论任务完成后，生成的 VOC 分析会自动出现在这里。</p></div>:
    selected&&<><div className="pa-voc-summary"><div className="pa-voc-run-heading"><span className={`pa-voc-status ${selected.status||''}`}>{vocStatus(selected.status)}</span><div><b>{selected.project_name||`Amazon VOC · ${selected.id.slice(0,8)}`}</b><small>任务 {selected.id} · revision {selected.current_revision||1} · 更新于 {dateText(selected.updated_at||selected.created_at)}</small></div></div>
     <div className="pa-voc-metrics">{coverage.map(([label,value])=><span key={label}><b>{value==null?'—':Number(value).toLocaleString('zh-CN')}</b><small>{label}</small></span>)}</div>
     {source.analysis_scope&&<p className="pa-voc-scope">分析范围：{source.analysis_scope}</p>}
     <div className="pa-voc-downloads">{reportReady&&<a href={artifact('html')} target="_blank" rel="noreferrer"><ArrowDownToLine size={14}/>单独打开 HTML 报告</a>}{selected.status==='completed'&&artifacts.includes('excel')&&<a href={artifact('excel')}><FileSpreadsheet size={14}/>下载 VOC Excel</a>}{selected.status==='completed'&&artifacts.includes('quality')&&<a href={artifact('quality')} target="_blank" rel="noreferrer">查看质量报告</a>}</div>
    </div>
    {reportReady?<div className="pa-voc-report"><div className="pa-voc-report-heading"><b>VOC 分析报告</b><span>{reportError?reportError:reportLoading?'正在加载报告…':'报告已嵌入当前页面'}</span></div>{reportHtml?<iframe key={`${selected.id}-${selected.current_revision||1}`} title="VOC 分析报告" sandbox="allow-scripts allow-downloads" srcDoc={reportHtml}/>:<div className="pa-voc-report-loading">{reportError||'正在读取报告内容…'}</div>}</div>:
     <div className="pa-linked-empty pa-voc-waiting"><BookOpenCheck size={23}/><b>{selected.status==='completed'?'当前任务没有可显示的 HTML 报告':`VOC 正在${vocStatus(selected.status)}，报告生成后会显示在这里`}</b><p>报告状态会自动刷新；不会在此页面重复启动分析。</p></div>}
    </>}
  </section>
 </main>;
}
