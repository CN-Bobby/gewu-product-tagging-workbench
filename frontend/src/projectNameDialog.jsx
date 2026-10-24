import React, { useState } from 'react';
import { X } from 'lucide-react';
import './projectNameDialog.css';

export function ProjectNameDialog({ api, project = null, onClose, onSaved }) {
  const [name, setName] = useState(project?.name || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const editing = !!project;

  const submit = async event => {
    event.preventDefault();
    const value = name.trim();
    if (!value || busy) return;
    setBusy(true);
    setError('');
    try {
      const saved = editing
        ? await api(`/api/projects/${encodeURIComponent(project.id)}`, { name: value }, 'PATCH')
        : await api('/api/projects', { name: value });
      onSaved?.(saved, editing);
      onClose();
    } catch (cause) {
      setError(cause.message || (editing ? '重命名项目失败' : '新建项目失败'));
    } finally {
      setBusy(false);
    }
  };

  return <div className="project-name-backdrop" onMouseDown={event => event.target === event.currentTarget && !busy && onClose()}>
    <form className="project-name-dialog" role="dialog" aria-modal="true" aria-labelledby="project-name-title" onSubmit={submit}>
      <header><b id="project-name-title">{editing ? '重命名项目' : '新建项目'}</b><button type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={16}/></button></header>
      <p>{editing ? '更改项目显示名称；项目资料和关联数据会保留。' : '为这个项目设置一个便于识别的名称。'}</p>
      <label>项目名称<input autoFocus aria-label="项目名称" value={name} onChange={event => setName(event.target.value)} maxLength={100} required/></label>
      {error && <div className="project-name-error" role="alert">{error}</div>}
      <footer><button type="button" disabled={busy} onClick={onClose}>取消</button><button className="primary" type="submit" disabled={busy || !name.trim()}>{busy ? '保存中…' : editing ? '保存名称' : '创建项目'}</button></footer>
    </form>
  </div>;
}
