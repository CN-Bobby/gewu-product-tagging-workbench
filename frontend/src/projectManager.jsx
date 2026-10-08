import React, { useEffect, useMemo, useState } from 'react';
import { Download, FileSpreadsheet, FileText, FolderOpen, RefreshCw, Search } from 'lucide-react';
import { filterProjectArtifacts, formatArtifactDate, formatFileSize, normalizeProjectLibrary } from './projectFilesState';
import { ProjectPreview } from './projectPreview';
import './projectManager.css';

export function ProjectManager({ api, route = {}, navigate = () => {} }) {
  const workspaceId = route.workspaceId || '';
  const [projects, setProjects] = useState([]);
  const [library, setLibrary] = useState(null);
  const [query, setQuery] = useState('');
  const [order, setOrder] = useState('newest');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => { let live = true; api('/api/projects', undefined, 'GET').then(rows => live && setProjects(Array.isArray(rows) ? rows : [])).catch(value => live && setError(value.message)); return () => { live = false; }; }, [api, reload]);
  useEffect(() => {
    if (!workspaceId) { setLibrary(null); return undefined; }
    let live = true; setLoading(true); setError(''); setLibrary(null);
    api(`/api/projects/${workspaceId}/library`, undefined, 'GET').then(value => live && setLibrary(value)).catch(value => live && setError(value.message)).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [api, workspaceId, reload]);

  const project = library?.project || projects.find(item => item.id === workspaceId);
  const folders = useMemo(() => normalizeProjectLibrary(library), [library]);
  const artifacts = folders.flatMap(folder => folder.items.map(item => ({ ...item, folder_label: folder.label })));
  const selected = artifacts.find(item => item.id === route.artifactId);
  const visible = useMemo(() => {
    const rows = filterProjectArtifacts(route.folderKind ? artifacts.filter(item => item.kind === route.folderKind) : artifacts, query);
    return order === 'oldest' ? [...rows].reverse() : rows;
  }, [artifacts, query, order, route.folderKind]);
  const relatedVersions = selected ? artifacts.filter(item => item.name === selected.name && item.kind === selected.kind).sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))) : [];
  const selectedFolder = folders.find(item => item.kind === route.folderKind);

  return <main className="project-manager">
    <header className="pm-heading"><div><div className="eyebrow">PROJECT LIBRARY</div><h1>{project?.name || (workspaceId ? '项目资料' : '项目资料')}</h1><p>项目资料、任务成品与可预览文件集中在这里。</p></div><button type="button" onClick={() => setReload(value => value + 1)} disabled={loading}><RefreshCw size={15}/>刷新</button></header>
    {error && <div className="pm-error" role="alert">{error}</div>}
    {!workspaceId ? <section className="pm-empty home-card"><FolderOpen size={30}/><h2>选择一个项目</h2><p>从左侧项目树选择项目或文件夹。</p></section> : <div className="pm-library-layout">
      <section className="pm-file-browser home-card">
        <div className="pm-browser-head"><div><small>{selectedFolder?.label || '项目概况'}</small><h2>{loading ? '正在加载…' : `${artifacts.length} 个成品`}</h2></div><label className="pm-sort">时间排序<select aria-label="时间排序" value={order} onChange={event => setOrder(event.target.value)}><option value="newest">最新优先</option><option value="oldest">最早优先</option></select></label></div>
        <label className="pm-search"><Search size={15}/><input aria-label="搜索项目文件" placeholder="搜索文件名或来源" value={query} onChange={event => setQuery(event.target.value)}/></label>
        <div className="pm-file-list">{loading ? <div className="pm-list-state">正在读取项目资料…</div> : !visible.length ? <div className="pm-list-state">{artifacts.length ? '没有匹配的文件。' : '这个项目还没有已归档成品。'}</div> : visible.map(item => <article className={`pm-file-row ${item.id === selected?.id ? 'active' : ''}`} key={item.id}>
          <button className="pm-file-select" onClick={() => navigate(`projects/${workspaceId}/files/${item.id}`)}><span className="pm-file-icon">{item.kind === 'data_x' ? <FileSpreadsheet size={16}/> : <FileText size={16}/>}</span><span className="pm-file-info"><b title={item.name}>{item.name}</b><small>{item.folder_label} · {formatArtifactDate(item.created_at)} · {formatFileSize(item.size)}</small><small>{item.source_type || '项目成品'} · {String(item.sha256 || '').slice(0, 12)}{item.sha256 ? '…' : ''}</small></span></button><span className="pm-file-actions">{item.previewable && <button type="button" onClick={() => navigate(`projects/${workspaceId}/files/${item.id}`)}>预览</button>}{item.downloadable && <a href={`/api/projects/${workspaceId}/artifacts/${item.id}/download`} download aria-label={`下载 ${item.name}`}><Download size={14}/></a>}</span>
        </article>)}</div>
      </section>
      <section className="pm-preview-panel home-card">
        {!route.artifactId ? <div className="pm-overview"><div className="pm-overview-icon"><FolderOpen size={25}/></div><h2>{project?.name || '项目概况'}</h2><p>项目固定包含四个资料分类。点击左侧文件夹查看成品，选择文件后可在此预览。</p><div className="pm-folder-summary">{folders.map(folder => <button key={folder.kind} onClick={() => navigate(`projects/${workspaceId}/folders/${folder.kind}`)}><span>{folder.label}</span><b>{folder.count}</b></button>)}</div></div> : selected ? <>
          <div className="pm-preview-title"><div><small>{selected.folder_label} · {formatFileSize(selected.size)}</small><h2 title={selected.name}>{selected.name}</h2></div>{selected.downloadable && <a className="pm-download" href={`/api/projects/${workspaceId}/artifacts/${selected.id}/download`} download><Download size={14}/>下载</a>}</div>
          {relatedVersions.length > 1 && <label className="pm-version-select">版本<select aria-label="文件版本" value={selected.id} onChange={event => navigate(`projects/${workspaceId}/files/${event.target.value}`)}>{relatedVersions.map((item,index) => <option key={item.id} value={item.id}>{index === 0 ? '最新版本' : `历史版本 ${index + 1}`} · {formatArtifactDate(item.created_at)}</option>)}</select></label>}
          <ProjectPreview workspaceId={workspaceId} item={selected}/>
        </> : <div className="pm-list-state">{loading ? '正在加载资料…' : '此文件不在项目资料索引中。'}</div>}
      </section>
    </div>}
  </main>;
}
