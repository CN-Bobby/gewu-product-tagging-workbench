export const PROJECT_FOLDERS = [
  { kind: 'source', label: '来源文件' },
  { kind: 'data_x', label: '数据' },
  { kind: 'reviews', label: '评论' },
  { kind: 'voc', label: 'VOC' },
  { kind: 'keywords', label: '关键词' },
  { kind: 'other', label: '其他' },
];

export function normalizeProjectLibrary(payload) {
  const supplied = new Map((payload?.folders || []).map(folder => [folder.kind, folder]));
  return PROJECT_FOLDERS.map(folder => ({
    ...folder,
    ...(supplied.get(folder.kind) || {}),
    kind: folder.kind,
    label: supplied.get(folder.kind)?.label || folder.label,
    items: supplied.get(folder.kind)?.items || [],
    count: Number(supplied.get(folder.kind)?.count ?? supplied.get(folder.kind)?.items?.length ?? 0),
  }));
}

export function filterProjectArtifacts(artifacts, query = '') {
  const needle = String(query || '').trim().toLocaleLowerCase();
  const rows = Array.isArray(artifacts) ? artifacts : [];
  return rows.filter(item => !item.deleted_at && item.status !== 'deleted')
    .filter(item => !needle || [item.name, item.filename, item.kind, item.source_type, item.source_id, item.source_identity]
      .some(value => String(value || '').toLocaleLowerCase().includes(needle)))
    .sort((a, b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')) || String(a.name).localeCompare(String(b.name)));
}

export function latestProjectArtifacts(artifacts, limit = 20) {
  return filterProjectArtifacts(artifacts).slice(0, limit);
}

export function artifactSourceIdentity(item) {
  return item?.source_identity || (item?.source_type && item?.source_id ? `${item.source_type}:${item.source_id}` : '');
}

export function artifactDownloadUrl(workspaceId, item) {
  return `/api/projects/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(item.id)}/download`;
}

export function artifactTaskHref(item) {
  if (item?.source_task_url) return item.source_task_url;
  if (item?.source_type === 'keyword_research' && item?.source_id) return `#/keyword/run/${encodeURIComponent(item.source_id)}`;
  return '';
}

export function formatFileSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '大小未知';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export function formatArtifactDate(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false });
}
