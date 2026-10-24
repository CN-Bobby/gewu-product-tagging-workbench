import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Download, Eye, FileText, Folder, FolderOpen, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, RotateCcw, Trash2, Upload, X } from 'lucide-react';
import { artifactDownloadUrl, latestProjectArtifacts, normalizeProjectLibrary } from './projectFilesState';
import { ProjectNameDialog } from './projectNameDialog';
import './projectSidebar.css';

const fileKinds = [
  ['source', '来源文件'], ['data_x', '数据'], ['reviews', '评论'],
  ['voc', 'VOC'], ['keywords', '关键词'], ['other', '其他'],
];
const itemName = item => item?.name || item?.filename || item?.original_name || '未命名文件';

async function uploadFile(workspaceId, file, kind, filename = file.name) {
  const form = new FormData();
  form.append('file', file, filename);
  form.append('kind', kind);
  const response = await fetch(`/api/projects/${encodeURIComponent(workspaceId)}/library/upload`, { method: 'POST', body: form });
  if (!response.ok) {
    let message = '上传失败';
    try { const body = await response.json(); message = body.detail?.message || body.detail || message; } catch { /* response may be text */ }
    throw new Error(message);
  }
  return response.json();
}

export function ProjectSidebar({ api, route, navigate, currentWorkspaceId = '' }) {
  const [projects, setProjects] = useState([]);
  const [trashProjects, setTrashProjects] = useState([]);
  const [libraries, setLibraries] = useState({});
  const [open, setOpen] = useState(() => localStorage.getItem('project-sidebar-open') !== 'false' && !window.matchMedia('(max-width: 760px)').matches);
  const userToggled = useRef(false);
  const [expanded, setExpanded] = useState({});
  const [trashExpanded, setTrashExpanded] = useState(false);
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [archiveWorkspaceId, setArchiveWorkspaceId] = useState('');
  const [reload, setReload] = useState(0);
  const [editItem, setEditItem] = useState(null);
  const [editName, setEditName] = useState('');
  const [editKind, setEditKind] = useState('other');
  const [targetWorkspace, setTargetWorkspace] = useState('');
  const [confirmItem, setConfirmItem] = useState(null);
  const [confirmProject, setConfirmProject] = useState(null);
  const [renameProject, setRenameProject] = useState(null);
  const [confirmError, setConfirmError] = useState('');
  const [uploadTarget, setUploadTarget] = useState(null);
  const [uploading, setUploading] = useState(false);
  const uploadRef = useRef(null);
  const activeId = route?.workspaceId || archiveWorkspaceId || currentWorkspaceId;

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError('');
    api('/api/projects', undefined, 'GET').then(async rows => {
      if (!live) return;
      const items = Array.isArray(rows) ? rows : [];
      setProjects(items);
      setExpanded(current => Object.fromEntries(items.map(project => [project.id, current[project.id] ?? true])));
      const results = await Promise.all(items.map(async project => {
        try { return [project.id, await api(`/api/projects/${encodeURIComponent(project.id)}/library`, undefined, 'GET'), '']; }
        catch (value) { return [project.id, null, value.message || '读取项目文件失败']; }
      }));
      if (!live) return;
      setLibraries(Object.fromEntries(results.filter(([, value]) => value).map(([id, value]) => [id, value])));
      const failed = results.find(([, , message]) => message);
      if (failed) setError(`${items.find(project => project.id === failed[0])?.name || '项目'}：${failed[2]}`);
    }).catch(value => live && setError(value.message || '读取项目失败')).finally(() => live && setLoading(false));
    api('/api/projects/trash', undefined, 'GET')
      .then(rows => live && setTrashProjects(Array.isArray(rows) ? rows : []))
      .catch(value => live && setError(value.message || '读取项目回收站失败'));
    return () => { live = false; };
  }, [api, reload]);

  useEffect(() => {
    if (route?.page !== 'product-archive' || !route.archiveId) {
      setArchiveWorkspaceId('');
      return undefined;
    }
    let live = true;
    api(`/api/product-archive/archives/${route.archiveId}`, undefined, 'GET')
      .then(value => live && setArchiveWorkspaceId(value?.workspace_id || ''))
      .catch(() => live && setArchiveWorkspaceId(''));
    return () => { live = false; };
  }, [api, route?.page, route?.archiveId]);

  useEffect(() => {
    const resize = () => { if (!userToggled.current && window.matchMedia('(max-width: 760px)').matches) setOpen(false); };
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  const refreshTree = () => setReload(value => value + 1);
  useEffect(() => {
    const refresh = () => setReload(value => value + 1);
    window.addEventListener('gewu-projects-changed', refresh);
    return () => window.removeEventListener('gewu-projects-changed', refresh);
  }, []);
  const create = async event => {
    event.preventDefault();
    if (!newName.trim() || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const project = await api('/api/projects', { name: newName.trim() });
      setNewName(''); setExpanded(value => ({ ...value, [project.id]: true }));
      window.dispatchEvent(new Event('gewu-projects-changed'));
      navigate(`projects/${project.id}`);
    } catch (value) { setError(value.message); }
    finally { setBusy(false); }
  };

  const beginUpload = (workspaceId, kind) => {
    setUploadTarget({ workspaceId, kind });
    setError(''); setNotice('');
    window.setTimeout(() => uploadRef.current?.click(), 0);
  };
  const onUpload = async event => {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file || !uploadTarget || uploading) return;
    if (!/\.(xlsx|html)$/i.test(file.name)) { setError('目前只支持上传 .xlsx 和 .html 文件。'); return; }
    setUploading(true); setError(''); setNotice('');
    try {
      try { await uploadFile(uploadTarget.workspaceId, file, uploadTarget.kind); }
      catch (value) {
        if (!String(value.message || '').includes('已存在')) throw value;
        const renamed = window.prompt('这个项目里已有同名文件。输入新文件名后重试，取消则放弃上传。', file.name);
        if (!renamed?.trim()) { setNotice('已取消上传，没有覆盖现有文件。'); return; }
        await uploadFile(uploadTarget.workspaceId, file, uploadTarget.kind, renamed.trim());
      }
      setNotice(`${file.name} 已加入项目资料。`); refreshTree();
    } catch (value) { setError(value.message || '上传失败'); }
    finally { setUploading(false); setUploadTarget(null); }
  };

  const startEdit = (item, workspaceId) => {
    if (item.downloadable === false) return;
    setEditItem({ ...item, workspaceId }); setEditName(itemName(item));
    setEditKind(item.kind || 'other'); setTargetWorkspace(workspaceId);
    setError(''); setNotice('');
  };
  const submitEdit = async event => {
    event.preventDefault();
    if (!editItem || busy || !editName.trim()) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api(`/api/projects/${encodeURIComponent(editItem.workspaceId)}/library/${encodeURIComponent(editItem.id)}`, {
        name: editName.trim(), kind: editKind,
        ...(targetWorkspace !== editItem.workspaceId ? { workspace_id: targetWorkspace } : {}),
      }, 'PATCH');
      setNotice(targetWorkspace !== editItem.workspaceId ? '文件已移动到目标项目。' : '文件信息已更新。');
      setEditItem(null); refreshTree();
    } catch (value) { setError(value.message || '保存失败'); }
    finally { setBusy(false); }
  };
  const moveToTrash = async () => {
    if (!confirmItem || busy || confirmItem.item.downloadable === false) return;
    const { item, workspaceId } = confirmItem;
    setBusy(true); setError(''); setNotice(''); setConfirmError('');
    try {
      await api(`/api/projects/${encodeURIComponent(workspaceId)}/library/${encodeURIComponent(item.id)}`, {}, 'DELETE');
      setNotice(`${itemName(item)} 已移入回收站，可在项目资料页恢复。`);
      setConfirmItem(null); refreshTree();
    } catch (value) { setConfirmError(value.message || '移入回收站失败'); }
    finally { setBusy(false); }
  };
  const moveProjectToTrash = async () => {
    if (!confirmProject || busy) return;
    const project = confirmProject;
    setBusy(true); setError(''); setNotice(''); setConfirmError('');
    try {
      await api(`/api/projects/${encodeURIComponent(project.id)}`, {}, 'DELETE');
      setConfirmProject(null);
      setNotice(`项目“${project.name}”已移入回收站，数据保留且可恢复。`);
      refreshTree();
      if (activeId === project.id) navigate('projects');
    } catch (value) { setConfirmError(value.message || '移入项目回收站失败'); }
    finally { setBusy(false); }
  };
  const restoreProject = async project => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api(`/api/projects/${encodeURIComponent(project.id)}/restore`, {}, 'POST');
      setNotice(`项目“${project.name}”已恢复到项目列表。`);
      refreshTree();
    } catch (value) { setError(value.message || '恢复项目失败'); }
    finally { setBusy(false); }
  };

  const toggleOpen = () => {
    userToggled.current = true;
    setOpen(value => { localStorage.setItem('project-sidebar-open', String(!value)); return !value; });
  };
  const projectRows = useMemo(() => projects.map(project => ({ project, folders: normalizeProjectLibrary(libraries[project.id]) })), [projects, libraries]);

  return <aside className={`project-sidebar ${open ? '' : 'is-collapsed'}`} aria-label="项目树">
    <div className="ps-heading"><b>项目</b><button type="button" aria-label={open ? '折叠项目树' : '展开项目树'} onClick={toggleOpen}>{open ? <PanelLeftClose size={16}/> : <PanelLeftOpen size={16}/>}</button></div>
    {!open && <button type="button" className={`ps-collapsed-library ${route?.page === 'projects' ? 'active' : ''}`} aria-label="项目资料" aria-current={route?.page === 'projects' ? 'page' : undefined} title="项目资料" onClick={() => navigate('projects')}><FolderOpen size={17}/><span>项目资料</span></button>}
    {open && <>
      <button type="button" className={`ps-all ${route?.page === 'projects' && !route?.workspaceId ? 'active' : ''}`} onClick={() => navigate('projects')}><FolderOpen size={15}/>项目资料</button>
      {error && <div className="ps-feedback ps-error" role="alert">{error}<button type="button" aria-label="关闭错误" onClick={() => setError('')}><X size={13}/></button></div>}
      {notice && <div className="ps-feedback ps-notice" role="status">{notice}<button type="button" aria-label="关闭提示" onClick={() => setNotice('')}><X size={13}/></button></div>}
      {loading && !projects.length && <p className="ps-empty" role="status">正在读取项目…</p>}
      <div className="ps-list">{projectRows.map(({ project, folders }) => <section className="ps-project" key={project.id}>
        <div className={`ps-project-row ${activeId === project.id ? 'active' : ''}`}>
          <button type="button" className="ps-tree-toggle" aria-label={`${expanded[project.id] ? '折叠' : '展开'}项目 ${project.name}`} aria-expanded={!!expanded[project.id]} onClick={() => setExpanded(value => ({ ...value, [project.id]: !value[project.id] }))}>{expanded[project.id] ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button>
          <button type="button" className="ps-project-link" title={project.name} onClick={() => { setExpanded(value => ({ ...value, [project.id]: true })); navigate(`projects/${project.id}`); }}><Folder size={15}/><span>{project.name}</span></button>
          <button type="button" className="ps-icon-button" aria-label={`重命名项目：${project.name}`} title="重命名项目" disabled={busy || uploading} onClick={() => setRenameProject(project)}><Pencil size={13}/></button>
          <button type="button" className="ps-icon-button ps-project-upload" aria-label={`向项目 ${project.name} 上传文件`} title="上传到项目" disabled={uploading || busy} onClick={() => beginUpload(project.id, 'source')}><Upload size={13}/></button>
          <button type="button" className="ps-icon-button ps-project-trash" aria-label={`将项目移入回收站：${project.name}`} title="移入回收站" disabled={busy || uploading} onClick={() => { setConfirmError(''); setConfirmProject(project); }}><Trash2 size={13}/></button>
        </div>
        {expanded[project.id] && <div className="ps-folders">{folders.map(folder => {
          const recent = latestProjectArtifacts(folder.items, 20);
          return <div className="ps-folder-group" key={folder.kind}>
            <div className={`ps-folder-row ${route?.workspaceId === project.id && (route?.folderKind === folder.kind || route?.artifactId && folder.items.some(item => item.id === route.artifactId)) ? 'active' : ''}`}>
              <button type="button" className="ps-folder-link" onClick={() => navigate(`projects/${project.id}/folders/${folder.kind}`)}><Folder size={13}/><span>{folder.label}</span><small>{folder.count}</small></button>
              <button type="button" className="ps-icon-button ps-folder-upload" aria-label={`向${project.name}的${folder.label}上传文件`} title={`上传到${folder.label}`} disabled={uploading || busy} onClick={() => beginUpload(project.id, folder.kind)}><Plus size={12}/></button>
            </div>
            <div className="ps-recent-files">{recent.map(item => <div className={`ps-file-row ${route?.artifactId === item.id ? 'active' : ''}`} key={item.id}>
              <button type="button" className="ps-recent-file" title={itemName(item)} onClick={() => navigate(`projects/${project.id}/files/${item.id}`)}><FileText size={12}/><span>{itemName(item)}</span></button>
              <details className="ps-file-menu"><summary aria-label={`文件操作：${itemName(item)}`} title="文件操作"><MoreHorizontal size={14}/></summary><div className="ps-menu-popover" role="group" aria-label={`${itemName(item)}的操作`}>
                <button type="button" onClick={() => navigate(`projects/${project.id}/files/${item.id}`)}><Eye size={13}/>预览</button>
                {item.downloadable !== false && <a href={artifactDownloadUrl(project.id, item)} download><Download size={13}/>下载</a>}
                {item.downloadable !== false && <><button type="button" onClick={() => startEdit(item, project.id)}><Pencil size={13}/>重命名 / 分类 / 移动</button><button type="button" className="ps-menu-danger" onClick={() => { setConfirmError(''); setConfirmItem({ item, workspaceId: project.id }); }}><Trash2 size={13}/>移入回收站</button></>}
                {item.downloadable === false && <span className="ps-readonly-note">历史登记项仅支持预览导航</span>}
              </div></details>
            </div>)}{folder.count > 20 && <button type="button" className="ps-view-all" onClick={() => navigate(`projects/${project.id}/folders/${folder.kind}`)}>查看全部 · {folder.count}</button>}{folder.count > 0 && folder.count <= 20 && <button type="button" className="ps-view-all" onClick={() => navigate(`projects/${project.id}/folders/${folder.kind}`)}>查看全部</button>}{!folder.count && <span className="ps-no-files">暂无文件</span>}</div>
          </div>;
        })}</div>}
      </section>)}</div>
      {!projects.length && !loading && !error && <p className="ps-empty">还没有项目</p>}
      <section className="ps-project-trash-list">
        <button type="button" className="ps-trash-toggle" aria-expanded={trashExpanded} onClick={() => setTrashExpanded(value => !value)}><Trash2 size={14}/><span>项目回收站</span><small>{trashProjects.length}</small>{trashExpanded ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button>
        {trashExpanded && <div className="ps-trash-items">{trashProjects.length ? trashProjects.map(project => <div className="ps-trash-item" key={project.id} title={project.name}><span>{project.name}</span><button type="button" aria-label={`恢复项目：${project.name}`} title="恢复项目" disabled={busy} onClick={() => restoreProject(project)}><RotateCcw size={12}/></button></div>) : <p className="ps-empty">回收站为空</p>}</div>}
      </section>
      <form className="ps-create" onSubmit={create}><input aria-label="新项目名称" value={newName} onChange={event => setNewName(event.target.value)} placeholder="新项目名称" maxLength={100}/><button type="submit" aria-label="新建项目" disabled={!newName.trim() || busy}><Plus size={15}/></button></form>
      <input ref={uploadRef} className="ps-hidden-input" type="file" accept=".xlsx,.html" onChange={onUpload} aria-label="选择上传文件"/>
    </>}
    {renameProject && <ProjectNameDialog api={api} project={renameProject} onClose={() => setRenameProject(null)} onSaved={saved => { setNotice(`项目名称已更新为“${saved.name}”。`); window.dispatchEvent(new Event('gewu-projects-changed')); }}/>}
    {editItem && <div className="ps-modal-backdrop"><form className="ps-modal" role="dialog" aria-modal="true" aria-labelledby="ps-edit-title" onSubmit={submitEdit}>
      <header><b id="ps-edit-title">整理文件</b><button type="button" aria-label="关闭" onClick={() => setEditItem(null)}><X size={16}/></button></header>
      <label>文件名<input autoFocus value={editName} onChange={event => setEditName(event.target.value)} required maxLength={255}/></label>
      <label>分类<select value={editKind} onChange={event => setEditKind(event.target.value)}>{fileKinds.map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</select></label>
      <label>所属项目<select value={targetWorkspace} onChange={event => setTargetWorkspace(event.target.value)}>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <footer><button type="button" onClick={() => setEditItem(null)}>取消</button><button className="ps-primary" type="submit" disabled={busy || !editName.trim()}>{busy ? '保存中…' : '保存'}</button></footer>
    </form></div>}
    {confirmItem && <div className="ps-modal-backdrop"><section className="ps-modal" role="dialog" aria-modal="true" aria-labelledby="ps-trash-title"><header><b id="ps-trash-title">移入回收站</b><button type="button" aria-label="关闭" onClick={() => setConfirmItem(null)}><X size={16}/></button></header><p>确定将“{itemName(confirmItem.item)}”移入回收站吗？之后可以在项目资料页恢复。</p>{confirmError && <p className="ps-modal-error" role="alert">操作失败：{confirmError}</p>}<footer><button type="button" onClick={() => setConfirmItem(null)}>取消</button><button className="ps-danger" type="button" disabled={busy} onClick={moveToTrash}>{busy ? '处理中…' : '移入回收站'}</button></footer></section></div>}
    {confirmProject && <div className="ps-modal-backdrop"><section className="ps-modal" role="dialog" aria-modal="true" aria-labelledby="ps-project-trash-title"><header><b id="ps-project-trash-title">移入项目回收站</b><button type="button" aria-label="关闭" onClick={() => setConfirmProject(null)}><X size={16}/></button></header><p>确定将项目“{confirmProject.name}”移入回收站吗？仅移入回收站，项目数据保留且可恢复。</p>{confirmError && <p className="ps-modal-error" role="alert">操作失败：{confirmError}</p>}<footer><button type="button" onClick={() => setConfirmProject(null)}>取消</button><button className="ps-danger" type="button" disabled={busy} onClick={moveProjectToTrash}>{busy ? '处理中…' : '移入回收站'}</button></footer></section></div>}
  </aside>;
}
