import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Download, FileSpreadsheet, FileText, FolderOpen, RefreshCw, Search, Upload, RotateCcw, Pencil, Trash2, FolderInput, X } from 'lucide-react';
import { artifactDownloadUrl, artifactSourceIdentity, artifactTaskHref, filterProjectArtifacts, formatArtifactDate, formatFileSize, normalizeProjectLibrary } from './projectFilesState';
import { ProjectPreview } from './projectPreview';
import './projectManager.css';

const kinds = [['source','来源文件'],['data_x','数据'],['reviews','评论'],['voc','VOC'],['keywords','关键词'],['other','其他']];
const basename = item => item?.name || item?.filename || item?.original_name || '未命名文件';

async function uploadFile(workspaceId, file, kind, filename = file.name) {
  const form = new FormData(); form.append('file', file, filename); form.append('kind', kind);
  const response = await fetch(`/api/projects/${encodeURIComponent(workspaceId)}/library/upload`, { method: 'POST', body: form });
  if (!response.ok) { let detail = '上传失败'; try { const body = await response.json(); detail = body.detail?.message || body.detail || detail; } catch { /* response may be text */ } throw new Error(detail); }
  return response.json();
}

export function ProjectManager({ api, route = {}, navigate = () => {} }) {
  const workspaceId = route.workspaceId || '';
  const [projects, setProjects] = useState([]), [library, setLibrary] = useState(null), [trash, setTrash] = useState([]);
  const [query, setQuery] = useState(''), [kindFilter, setKindFilter] = useState('all'), [order, setOrder] = useState('newest');
  const [loading, setLoading] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState(''), [reload, setReload] = useState(0);
  const [selectedIds, setSelectedIds] = useState([]), [showTrash, setShowTrash] = useState(false), [editItem, setEditItem] = useState(null), [editName, setEditName] = useState(''), [editKind, setEditKind] = useState('other'), [targetWorkspace, setTargetWorkspace] = useState('');
  const [busy, setBusy] = useState(false), [uploadKind, setUploadKind] = useState('source');
  const uploadRef = useRef(null);

  useEffect(() => { let live = true; api('/api/projects', undefined, 'GET').then(rows => live && setProjects(Array.isArray(rows) ? rows : [])).catch(value => live && setError(value.message)); return () => { live = false; }; }, [api, reload]);
  useEffect(() => {
    if (!workspaceId) { setLibrary(null); setTrash([]); setSelectedIds([]); return undefined; }
    let live = true; setLoading(true); setError(''); setNotice(''); setLibrary(null); setSelectedIds([]);
    Promise.all([api(`/api/projects/${workspaceId}/library`, undefined, 'GET'), api(`/api/projects/${workspaceId}/trash`, undefined, 'GET').catch(() => ({ items: [] }))])
      .then(([value, removed]) => { if (!live) return; setLibrary(value); setTrash(removed?.items || []); })
      .catch(value => live && setError(value.message)).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [api, workspaceId, reload]);

  const project = library?.project || projects.find(item => item.id === workspaceId);
  const folders = useMemo(() => normalizeProjectLibrary(library), [library]);
  const artifacts = useMemo(() => folders.flatMap(folder => folder.items.map(item => ({ ...item, folder_label: folder.label }))), [folders]);
  const selected = artifacts.find(item => item.id === route.artifactId);
  const inTrash = showTrash || route.page === 'project-trash';
  const sourceRows = inTrash ? trash : artifacts.filter(item => !route.folderKind || item.kind === route.folderKind);
  const visible = useMemo(() => {
    const rows = filterProjectArtifacts(sourceRows, query).filter(item => kindFilter === 'all' || item.kind === kindFilter);
    const ordered = [...rows].sort((a, b) => {
      if (order === 'name') return basename(a).localeCompare(basename(b), 'zh-CN');
      const aDate = String(a.updated_at || a.created_at || ''), bDate = String(b.updated_at || b.created_at || '');
      return order === 'oldest' ? aDate.localeCompare(bDate) : bDate.localeCompare(aDate);
    });
    return ordered;
  }, [sourceRows, query, kindFilter, order]);
  const versions = selected ? artifacts.filter(item => artifactSourceIdentity(item) && artifactSourceIdentity(item) === artifactSourceIdentity(selected)).sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))) : [];
  const selectedFolder = folders.find(item => item.kind === route.folderKind);
  const refresh = () => setReload(value => value + 1);
  const toggle = id => setSelectedIds(rows => rows.includes(id) ? rows.filter(value => value !== id) : [...rows, id]);

  const startEdit = item => { setEditItem(item); setEditName(basename(item)); setEditKind(item.kind || 'other'); setTargetWorkspace(workspaceId); };
  const submitEdit = async event => {
    event.preventDefault(); if (!editItem || busy) return;
    setBusy(true); setError('');
    try {
      await api(`/api/projects/${workspaceId}/library/${editItem.id}`, { name: editName.trim(), kind: editKind, ...(targetWorkspace !== workspaceId ? { workspace_id: targetWorkspace } : {}) }, 'PATCH');
      setEditItem(null); setNotice(targetWorkspace !== workspaceId ? '文件已移动到目标项目。' : '文件信息已更新。'); refresh();
    } catch (value) { setError(value.message); }
    finally { setBusy(false); }
  };
  const softDelete = async ids => {
    if (busy || !ids.length) return;
    setBusy(true); setError('');
    try { for (const id of ids) await api(`/api/projects/${workspaceId}/library/${id}`, {}, 'DELETE'); setSelectedIds([]); setNotice(`${ids.length} 个文件已移入回收站，可恢复。`); refresh(); }
    catch (value) { setError(value.message); refresh(); }
    finally { setBusy(false); }
  };
  const moveSelected = async () => {
    if (!selectedIds.length || !targetWorkspace || busy) return;
    setBusy(true); setError('');
    try { for (const id of selectedIds) await api(`/api/projects/${workspaceId}/library/${id}`, { workspace_id: targetWorkspace }, 'PATCH'); setNotice(`已移动 ${selectedIds.length} 个文件。`); setSelectedIds([]); refresh(); }
    catch (value) { setError(value.message); refresh(); }
    finally { setBusy(false); }
  };
  const restore = async id => {
    setBusy(true); setError('');
    try { await api(`/api/projects/${workspaceId}/trash/${id}/restore`, {}, 'POST'); setNotice('文件已恢复到项目资料。'); refresh(); }
    catch (value) { setError(value.message); }
    finally { setBusy(false); }
  };
  const onUpload = async event => {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file || busy) return;
    if (!/\.(xlsx|html)$/i.test(file.name)) { setError('目前只支持上传 .xlsx 和 .html 文件。'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      try { await uploadFile(workspaceId, file, uploadKind); }
      catch (value) {
        if (!String(value.message || '').includes('已存在')) throw value;
        const renamed = window.prompt('这个项目里已有同名文件。输入新文件名后重试，取消则放弃上传。', file.name);
        if (!renamed?.trim()) { setNotice('已取消上传，没有覆盖现有文件。'); return; }
        await uploadFile(workspaceId, file, uploadKind, renamed.trim());
      }
      setNotice(`${file.name} 已加入项目资料。`); refresh();
    }
    catch (value) { setError(value.message); }
    finally { setBusy(false); }
  };

  return <main className="project-manager">
    <header className="pm-heading"><div><div className="eyebrow">PROJECT LIBRARY</div><h1>{project?.name || '项目资料'}</h1><p>浏览、整理项目文件，并从来源记录跳转回生成任务。</p></div><div className="pm-heading-actions"><button type="button" onClick={() => { setShowTrash(value => !value); setSelectedIds([]); }} className={inTrash ? 'is-active' : ''}>{inTrash ? '返回项目文件' : `回收站 ${trash.length ? `(${trash.length})` : ''}`}</button><button type="button" onClick={refresh} disabled={loading}><RefreshCw size={15}/>刷新</button></div></header>
    {error && <div className="pm-error" role="alert">{error}<button aria-label="关闭错误" onClick={() => setError('')}><X size={14}/></button></div>}{notice && <div className="pm-notice">{notice}<button aria-label="关闭提示" onClick={() => setNotice('')}><X size={14}/></button></div>}
    {!workspaceId ? <section className="pm-empty home-card"><FolderOpen size={30}/><h2>选择一个项目</h2><p>从左侧项目树选择项目或文件夹。</p></section> : <div className="pm-library-layout">
      <section className="pm-file-browser home-card">
        <div className="pm-browser-head"><div><small>{inTrash ? '可恢复的已删除文件' : selectedFolder?.label || '项目文件'}</small><h2>{loading ? '正在加载…' : `${visible.length} 个文件`}</h2></div><label className="pm-sort">排序<select aria-label="文件排序" value={order} onChange={event => setOrder(event.target.value)}><option value="newest">最新优先</option><option value="oldest">最早优先</option><option value="name">文件名</option></select></label></div>
        <div className="pm-toolbar"><label className="pm-search"><Search size={15}/><input aria-label="搜索项目文件" placeholder="搜索文件名或来源" value={query} onChange={event => setQuery(event.target.value)}/></label><label className="pm-kind-filter"><span>分类</span><select aria-label="文件分类筛选" value={kindFilter} onChange={event => setKindFilter(event.target.value)}><option value="all">全部</option>{kinds.map(([kind,label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><label className="pm-upload-kind"><span>上传分类</span><select aria-label="上传文件分类" value={uploadKind} onChange={event => setUploadKind(event.target.value)}>{kinds.map(([kind,label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><input ref={uploadRef} type="file" accept=".xlsx,.html" hidden onChange={onUpload}/><button type="button" className="pm-upload" disabled={busy} onClick={() => uploadRef.current?.click()}><Upload size={14}/>上传文件</button></div>
        {selectedIds.length > 0 && !inTrash && <div className="pm-batch"><span>已选 {selectedIds.length} 项</span><label>移动到<select aria-label="批量移动目标项目" value={targetWorkspace} onChange={event => setTargetWorkspace(event.target.value)}><option value="">选择项目</option>{projects.filter(item => item.id !== workspaceId).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><button disabled={!targetWorkspace || busy} onClick={moveSelected}><FolderInput size={14}/>移动</button><button className="pm-danger" disabled={busy} onClick={() => softDelete(selectedIds)}><Trash2 size={14}/>移入回收站</button><button aria-label="清空选择" onClick={() => setSelectedIds([])}><X size={14}/></button></div>}
        <div className="pm-file-list">{loading ? <div className="pm-list-state">正在读取项目资料…</div> : !visible.length ? <div className="pm-list-state">{inTrash ? '回收站为空。' : artifacts.length ? '没有匹配的文件。' : '这个项目还没有文件。'}</div> : visible.map(item => {
          const identity = artifactSourceIdentity(item), sameSource = identity ? artifacts.filter(candidate => artifactSourceIdentity(candidate) === identity) : [];
          const taskHref = artifactTaskHref(item);
          return <article className={`pm-file-row ${item.id === selected?.id ? 'active' : ''}`} key={item.id}>
            {!inTrash && <input aria-label={`选择 ${basename(item)}`} type="checkbox" checked={selectedIds.includes(item.id)} onChange={() => toggle(item.id)}/>}
            <button className="pm-file-select" onClick={() => inTrash ? null : navigate(`projects/${workspaceId}/files/${item.id}`)}><span className="pm-file-icon">{/\.xlsx$/i.test(basename(item)) || item.kind === 'data_x' ? <FileSpreadsheet size={16}/> : <FileText size={16}/>}</span><span className="pm-file-info"><b title={basename(item)}>{basename(item)}</b><small>{kinds.find(([kind]) => kind === item.kind)?.[1] || item.kind || '其他'} · {formatArtifactDate(item.created_at)} · {formatFileSize(item.size)}</small><small>{identity ? `来源 ${identity} · ${sameSource.length} 个版本` : item.source_type || '项目文件'}{item.sha256 ? ` · ${String(item.sha256).slice(0, 12)}…` : ''}</small></span></button>
            <span className="pm-file-actions">{taskHref && <a href={taskHref} target={taskHref.startsWith('#') ? undefined : '_self'}>来源任务</a>}{inTrash ? <button type="button" disabled={busy} onClick={() => restore(item.id)}><RotateCcw size={14}/>恢复</button> : <><button type="button" aria-label={`重命名或移动 ${basename(item)}`} onClick={() => startEdit(item)}><Pencil size={14}/></button>{item.downloadable !== false && <a href={artifactDownloadUrl(workspaceId, item)} download aria-label={`下载 ${basename(item)}`}><Download size={14}/></a>}</>}</span>
          </article>;
        })}</div>
      </section>
      <section className="pm-preview-panel home-card">
        {inTrash ? <div className="pm-overview"><div className="pm-overview-icon"><RotateCcw size={25}/></div><h2>回收站</h2><p>文件可以恢复到项目资料。当前不提供永久删除。</p></div> : !route.artifactId ? <div className="pm-overview"><div className="pm-overview-icon"><FolderOpen size={25}/></div><h2>{project?.name || '项目概况'}</h2><p>项目资料按六类整理。选择文件后可预览，也可跳转回它的来源任务。</p><button type="button" className="pm-usage-link" onClick={() => navigate(`usage/${encodeURIComponent(workspaceId)}`)}>查看这个项目的用量统计 <span>↗</span></button><div className="pm-folder-summary">{folders.map(folder => <button key={folder.kind} onClick={() => navigate(`projects/${workspaceId}/folders/${folder.kind}`)}><span>{folder.label}</span><b>{folder.count}</b></button>)}</div></div> : selected ? <>
          <div className="pm-preview-title"><div><small>{selected.folder_label} · {formatFileSize(selected.size)}</small><h2 title={basename(selected)}>{basename(selected)}</h2></div>{selected.downloadable !== false && <a className="pm-download" href={artifactDownloadUrl(workspaceId, selected)} download><Download size={14}/>下载</a>}</div>
          {versions.length > 1 && <label className="pm-version-select">同一来源版本<select aria-label="同一来源版本" value={selected.id} onChange={event => navigate(`projects/${workspaceId}/files/${event.target.value}`)}>{versions.map((item,index) => <option key={item.id} value={item.id}>{index === 0 ? '最新版本' : `历史版本 ${index + 1}`} · {formatArtifactDate(item.created_at)}</option>)}</select></label>}
          <ProjectPreview workspaceId={workspaceId} item={selected}/>
        </> : <div className="pm-list-state">{loading ? '正在加载资料…' : '此文件不在项目资料索引中。'}</div>}
      </section>
    </div>}
    {editItem && <div className="pm-modal-backdrop"><form className="pm-edit-modal" onSubmit={submitEdit}><header><b>整理文件</b><button type="button" aria-label="关闭" onClick={() => setEditItem(null)}><X size={16}/></button></header><label>文件名<input autoFocus value={editName} onChange={event => setEditName(event.target.value)} required maxLength={255}/></label><label>分类<select value={editKind} onChange={event => setEditKind(event.target.value)}>{kinds.map(([kind,label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><label>所属项目<select value={targetWorkspace} onChange={event => setTargetWorkspace(event.target.value)}>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><footer><button type="button" onClick={() => setEditItem(null)}>取消</button><button className="primary" disabled={busy || !editName.trim()}>{busy ? '保存中…' : '保存'}</button></footer></form></div>}
  </main>;
}
