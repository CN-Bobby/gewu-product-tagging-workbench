import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Folder, FolderOpen, Plus, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { normalizeProjectLibrary } from './projectFilesState';
import './projectSidebar.css';

export function ProjectSidebar({ api, route, navigate, currentWorkspaceId = '' }) {
  const [projects, setProjects] = useState([]);
  const [libraries, setLibraries] = useState({});
  const [open, setOpen] = useState(() => localStorage.getItem('project-sidebar-open') !== 'false' && !window.matchMedia('(max-width: 760px)').matches);
  const userToggled = useRef(false);
  const [expanded, setExpanded] = useState({});
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [archiveWorkspaceId, setArchiveWorkspaceId] = useState('');
  const activeId = route?.workspaceId || archiveWorkspaceId || currentWorkspaceId;

  useEffect(() => { let live = true; api('/api/projects', undefined, 'GET').then(rows => {
    if (!live) return;
    const items = Array.isArray(rows) ? rows : [];
    setProjects(items);
    setExpanded(current => Object.fromEntries(items.map(project => [project.id, current[project.id] ?? true])));
    items.forEach(project => api(`/api/projects/${project.id}/library`, undefined, 'GET')
      .then(value => live && setLibraries(current => ({ ...current, [project.id]: value })))
      .catch(() => {}));
  }).catch(value => live && setError(value.message)); return () => { live = false; }; }, [api]);

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

  useEffect(() => { const resize = () => { if (!userToggled.current && window.matchMedia('(max-width: 760px)').matches) setOpen(false); }; window.addEventListener('resize', resize); return () => window.removeEventListener('resize', resize); }, []);

  const create = async event => {
    event.preventDefault();
    if (!newName.trim() || busy) return;
    setBusy(true); setError('');
    try {
      const project = await api('/api/projects', { name: newName.trim() });
      setProjects(items => [...items, project]);
      setExpanded(items => ({ ...items, [project.id]: true }));
      setNewName('');
      navigate(`projects/${project.id}`);
      api(`/api/projects/${project.id}/library`, undefined, 'GET').then(value => setLibraries(items => ({ ...items, [project.id]: value }))).catch(() => {});
    } catch (value) { setError(value.message); }
    finally { setBusy(false); }
  };

  const toggleOpen = () => { userToggled.current = true; setOpen(value => { localStorage.setItem('project-sidebar-open', String(!value)); return !value; }); };
  const projectRows = useMemo(() => projects.map(project => ({ project, folders: normalizeProjectLibrary(libraries[project.id]) })), [projects, libraries]);

  return <aside className={`project-sidebar ${open ? '' : 'is-collapsed'}`} aria-label="项目树">
    <div className="ps-heading"><b>项目</b><button type="button" aria-label={open ? '折叠项目树' : '展开项目树'} onClick={toggleOpen}>{open ? <PanelLeftClose size={16}/> : <PanelLeftOpen size={16}/>}</button></div>
    {open && <>
      <button className={`ps-all ${route?.page === 'projects' && !route?.workspaceId ? 'active' : ''}`} onClick={() => navigate('projects')}><FolderOpen size={15}/>项目资料</button>
      <div className="ps-list">{projectRows.map(({ project, folders }) => <section className="ps-project" key={project.id}>
        <button className={`ps-project-row ${activeId === project.id ? 'active' : ''}`} title={project.name} onClick={() => { setExpanded(value => ({ ...value, [project.id]: true })); navigate(`projects/${project.id}`); }}>
          <span className="ps-tree-toggle" onClick={event => { event.stopPropagation(); setExpanded(value => ({ ...value, [project.id]: !value[project.id] })); }}>{expanded[project.id] ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</span><Folder size={15}/><span>{project.name}</span>
        </button>
        {expanded[project.id] && <div className="ps-folders">{folders.map(folder => <button key={folder.kind} className={`ps-folder ${route?.workspaceId === project.id && (route?.folderKind === folder.kind || route?.artifactId && folder.items.some(item => item.id === route.artifactId)) ? 'active' : ''}`} onClick={() => navigate(`projects/${project.id}/folders/${folder.kind}`)}><Folder size={13}/><span>{folder.label}</span><small>{folder.count}</small></button>)}</div>}
      </section>)}</div>
      {!projects.length && !error && <p className="ps-empty">还没有项目</p>}
      {error && <p className="ps-error" role="alert">{error}</p>}
      <form className="ps-create" onSubmit={create}><input aria-label="新项目名称" value={newName} onChange={event => setNewName(event.target.value)} placeholder="新项目名称" maxLength={100}/><button aria-label="新建项目" disabled={!newName.trim() || busy}><Plus size={15}/></button></form>
    </>}
  </aside>;
}
