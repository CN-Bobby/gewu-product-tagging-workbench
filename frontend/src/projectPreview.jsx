import React, { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { formatFileSize } from './projectFilesState';
import './projectPreview.css';

async function readResponse(response) {
  if (response.ok) return response;
  let message = `读取失败（HTTP ${response.status}）`;
  try { const body = await response.json(); message = body.detail?.message || body.detail || message; } catch { /* response may be plain text */ }
  throw new Error(message);
}

function WorkbookPreview({ workspaceId, item }) {
  const [workbook, setWorkbook] = useState(null);
  const [sheet, setSheet] = useState('');
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => { setWorkbook(null); setSheet(''); setOffset(0); setQuery(''); }, [item.id]);
  useEffect(() => {
    let live = true; setLoading(true); setError('');
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ offset: String(offset), limit: '50', query });
        if (sheet) params.set('sheet', sheet);
        const response = await readResponse(await fetch(`/api/projects/${workspaceId}/library/${item.id}/workbook?${params}`));
        const value = await response.json();
        if (!live) return;
        setWorkbook(value);
        if (!sheet && value.sheet) setSheet(value.sheet);
      } catch (value) { if (live) setError(value.message); }
      finally { if (live) setLoading(false); }
    }, query ? 180 : 0);
    return () => { live = false; clearTimeout(timer); };
  }, [workspaceId, item.id, sheet, offset, query]);
  const headers = workbook?.headers || [];
  const rows = workbook?.rows || [];
  const total = Number(workbook?.total || 0);
  const imageUrl = imageId => `/api/projects/${workspaceId}/library/${item.id}/images/${encodeURIComponent(imageId)}`;
  return <div className="pp-workbook">
    <div className="pp-toolbar">{(workbook?.sheets?.length > 0) && <label>工作表<select aria-label="工作表" value={workbook?.sheet || sheet} onChange={event => { setOffset(0); setSheet(event.target.value); }}>{workbook.sheets.map(value => <option key={value} value={value}>{value}</option>)}</select></label>}<label className="pp-search"><Search size={14}/><input aria-label="搜索工作簿" placeholder="搜索当前工作表" value={query} onChange={event => { setOffset(0); setQuery(event.target.value); }}/></label><span>{loading ? '读取中…' : `${total} 行`}</span></div>
    {error ? <div className="pp-state" role="alert">{error}</div> : <div className="pp-table-wrap"><table className="product-table pp-table"><thead><tr><th className="pp-row-number">行</th>{headers.map((header,index) => <th key={`${header}-${index}`}>{header}</th>)}</tr></thead><tbody>{rows.map((row,index) => <tr key={row.excel_row ?? `${offset}-${index}`}><td className="pp-row-number">{row.excel_row ?? offset + index + 1}</td>{headers.map((header,columnIndex) => {
      const cell = row.values?.[columnIndex] ?? row.values?.[header] ?? '';
      const images = (row.images || []).filter(image => {
        const value = image.column;
        const letters = String(value || '').toUpperCase();
        const letterIndex = /^[A-Z]+$/.test(letters) ? [...letters].reduce((total, char) => total * 26 + char.charCodeAt(0) - 64, 0) - 1 : -1;
        return value === columnIndex || value === header || String(value) === String(columnIndex + 1) || letterIndex === columnIndex;
      });
      return <td key={`${header}-${columnIndex}`}>{cell != null && String(cell) !== '' && <span>{String(cell)}</span>}{images.map(image => <img className="pp-cell-image" key={image.image_id} src={imageUrl(image.image_id)} alt={`${header} 图片`} loading="lazy"/>)}</td>;
    })}</tr>)}</tbody></table>{!rows.length && !loading && <div className="pp-state">没有匹配的数据行。</div>}</div>}
    <div className="pp-pagination"><span>第 {total ? offset + 1 : 0}–{Math.min(offset + rows.length, total)} 行，共 {total} 行</span><div><button aria-label="上一页" disabled={offset <= 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 50))}><ChevronLeft size={15}/>上一页</button><button aria-label="下一页" disabled={offset + 50 >= total || loading} onClick={() => setOffset(value => value + 50)}>下一页<ChevronRight size={15}/></button></div></div>
  </div>;
}

function HtmlPreview({ workspaceId, item }) {
  const [source, setSource] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { let live = true; setSource(''); setError(''); fetch(`/api/projects/${workspaceId}/library/${item.id}/html`).then(readResponse).then(response => response.text()).then(value => live && setSource(value)).catch(value => live && setError(value.message)); return () => { live = false; }; }, [workspaceId, item.id]);
  if (error) return <div className="pp-state" role="alert">{error}</div>;
  if (!source) return <div className="pp-state">正在读取 HTML 报告…</div>;
  return <iframe className="pp-html" title={`${item.name} 预览`} sandbox="" srcDoc={source}/>;
}

export function ProjectPreview({ workspaceId, item }) {
  if (item.kind === 'voc' && item.name?.toLowerCase().endsWith('.html')) return <HtmlPreview workspaceId={workspaceId} item={item}/>;
  if (item.previewable && /\.(xlsx|xlsm|xls)$/i.test(item.name || '')) return <WorkbookPreview workspaceId={workspaceId} item={item}/>;
  return <div className="pp-state pp-unavailable">{item.previewable ? '暂不支持此文件类型的预览。' : '此文件暂不支持在线预览。'}<small>{formatFileSize(item.size)} · SHA-256 {item.sha256 || '未知'}</small></div>;
}
