export const PROJECT_FOLDERS = [
  { kind: 'source', label: '原始上传数据' },
  { kind: 'data_x', label: '打标结果' },
  { kind: 'reviews', label: '评论文件' },
  { kind: 'voc', label: 'VOC 报告' },
];

export function normalizeProjectLibrary(payload) {
  const supplied = new Map((payload?.folders || []).map(folder => [folder.kind, folder]));
  return PROJECT_FOLDERS.map(folder => ({
    ...folder,
    ...(supplied.get(folder.kind) || {}),
    kind: folder.kind,
    label: folder.label,
    items: supplied.get(folder.kind)?.items || [],
    count: Number(supplied.get(folder.kind)?.count ?? supplied.get(folder.kind)?.items?.length ?? 0),
  }));
}

export function filterProjectArtifacts(artifacts, query = '') {
  const needle = String(query || '').trim().toLocaleLowerCase();
  const rows = Array.isArray(artifacts) ? artifacts : [];
  return rows.filter(item => !needle || [item.name, item.kind, item.source_type]
    .some(value => String(value || '').toLocaleLowerCase().includes(needle)))
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || String(a.name).localeCompare(String(b.name)));
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
