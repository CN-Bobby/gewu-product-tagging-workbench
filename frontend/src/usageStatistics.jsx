import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import './usageStatistics.css';

const PAGE_SIZE = 30;
const localDate = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const today = () => localDate(new Date());
const daysAgo = days => { const date = new Date(); date.setDate(date.getDate() - days); return localDate(date); };
const number = value => Number(value || 0).toLocaleString();
const shortNumber = value => {
  const amount = Number(value || 0);
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M`;
  if (amount >= 10_000) return `${(amount / 1_000).toFixed(0)}K`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K`;
  return number(amount);
};
const tokenText = value => value == null ? '未知' : `${number(value)} tokens`;
const eventTokens = value => value == null ? '未知' : shortNumber(value);
const eventDateText = (value, timezone) => {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleString(undefined, timezone ? { timeZone: timezone } : undefined); }
  catch { return date.toLocaleString(); }
};
const queryString = values => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value != null) params.set(key, value);
  });
  return params.toString();
};
const eventQuality = quality => {
  if (quality === 'estimated') return '估算';
  if (quality === 'unknown') return '未知';
  if (quality === 'measured') return '实测';
  if (quality === 'not_applicable') return '不适用';
  return quality || '未标记';
};
const eventStatus = status => status === 'success' || status === 'succeeded' || status === 'completed' ? '成功' : status === 'failed' || status === 'error' || status === 'http_error' || status === 'network_error' ? '失败' : status === 'invalid' ? '格式无效' : status === 'in_progress' ? '进行中' : status === 'hit' ? '本地缓存命中' : status === 'miss' ? '本地缓存未命中' : status || '—';
const eventKind = kind => kind === 'model' ? '模型' : kind === 'mcp' ? 'MCP' : kind === 'local_cache' ? '本地缓存' : kind === 'agent_turn' ? 'Agent 回合' : kind || '—';

export function UsageStatistics({ api, projects = [], initialWorkspaceId = '', onProjectSelect = () => {} }) {
  const [rangeMode, setRangeMode] = useState('last30');
  const [start, setStart] = useState(daysAgo(29));
  const [end, setEnd] = useState(today());
  const [bucket, setBucket] = useState('day');
  const [filters, setFilters] = useState({ workspace_id: initialWorkspaceId || '', task_id: '', module: '', provider: '', model: '', function_name: '', quality: '' });
  const [summary, setSummary] = useState(null);
  const [events, setEvents] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState('');
  const [draftProjects, setDraftProjects] = useState({});
  const [loading, setLoading] = useState(false);
  const [savingTask, setSavingTask] = useState('');
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => setFilters(value => ({ ...value, workspace_id: initialWorkspaceId || '' })), [initialWorkspaceId]);

  const requestFilters = useMemo(() => ({
    range: rangeMode,
    bucket,
    ...(rangeMode === 'custom' ? { start, end } : {}),
    ...filters,
  }), [rangeMode, bucket, start, end, filters]);

  const refresh = useCallback(async () => {
    if (rangeMode === 'custom' && (!start || !end || start > end)) {
      setError('请填写有效的开始和结束日期。');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const summaryQuery = queryString(requestFilters);
      const eventQuery = queryString({ ...requestFilters, limit: PAGE_SIZE, offset });
      const [summaryData, eventData] = await Promise.all([
        api(`/api/usage/summary?${summaryQuery}`, undefined, 'GET'),
        api(`/api/usage/events?${eventQuery}`, undefined, 'GET'),
      ]);
      setSummary(summaryData || null);
      setEvents(eventData || { items: [], total: 0 });
    } catch (value) {
      setError(value?.message || '用量数据读取失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }, [api, requestFilters, offset, rangeMode, start, end, reload]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { const refreshAfterProjectRename = () => setReload(value => value + 1); window.addEventListener('gewu-projects-changed', refreshAfterProjectRename); return () => window.removeEventListener('gewu-projects-changed', refreshAfterProjectRename); }, []);

  const totals = summary?.totals || {};
  const series = summary?.series || [];
  const maxSeries = Math.max(1, ...series.map(item => Number(item.model_calls || 0) + Number(item.mcp_calls || 0)));
  const selectedProject = projects.find(project => String(project.id ?? project.workspace_id) === String(filters.workspace_id));
  const projectOptions = projects.map(project => ({ id: project.id ?? project.workspace_id, name: project.name ?? project.workspace_name ?? String(project.id) }));
  const setFilter = (name, value) => {
    setOffset(0);
    setFilters(current => ({ ...current, [name]: value }));
  };

  const saveTaskProject = async row => {
    const key = `${row.module || ''}:${row.task_id || ''}`;
    const workspaceId = draftProjects[key] ?? row.workspace_id ?? '';
    setSavingTask(key);
    setError('');
    try {
      await api(`/api/usage/tasks/${encodeURIComponent(row.module || '')}/${encodeURIComponent(row.task_id || '')}/project`, { workspace_id: workspaceId || null }, 'PUT');
      setDraftProjects(current => { const next = { ...current }; delete next[key]; return next; });
      setReload(value => value + 1);
    } catch (value) {
      setError(value?.message || '任务项目归属更新失败。');
    } finally {
      setSavingTask('');
    }
  };

  return <main className="usage-statistics">
    <header className="us-heading">
      <div><div className="us-eyebrow">USAGE · OBSERVABILITY</div><h1>用量统计</h1><p>汇总模型与 MCP 调用、Token 用量和任务明细，不展示金额。</p></div>
      <button className="us-refresh" type="button" onClick={() => setReload(value => value + 1)} disabled={loading}><RefreshCw size={14} className={loading ? 'us-spin' : ''}/>刷新数据</button>
    </header>

    <section className="us-filter-panel" aria-label="用量筛选">
      <div className="us-filter-row">
        <label>日期范围<select value={rangeMode} onChange={event => { setRangeMode(event.target.value); setOffset(0); }}><option value="today">今天</option><option value="this_week">本周（周一至周日）</option><option value="this_month">本月（完整自然月）</option><option value="last30">最近 30 天</option><option value="custom">自定义范围</option></select></label>
        {rangeMode === 'custom' && <><label>开始日期<input type="date" value={start} onChange={event => setStart(event.target.value)}/></label><label>结束日期<input type="date" value={end} onChange={event => setEnd(event.target.value)}/></label></>}
        <label>统计粒度<select value={bucket} onChange={event => setBucket(event.target.value)}><option value="day">按日</option><option value="week">按周</option><option value="month">按月</option></select></label>
        <label>项目<select value={filters.workspace_id} onChange={event => setFilter('workspace_id', event.target.value)}><option value="">所有项目</option>{projectOptions.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        <label>任务 ID<input value={filters.task_id} onChange={event => setFilter('task_id', event.target.value)} placeholder="筛选任务"/></label>
        <label>模块<input value={filters.module} onChange={event => setFilter('module', event.target.value)} placeholder="如 amazon_reviews"/></label>
        <label>服务商<input value={filters.provider} onChange={event => setFilter('provider', event.target.value)} placeholder="筛选服务商"/></label>
        <label>模型<input value={filters.model} onChange={event => setFilter('model', event.target.value)} placeholder="筛选模型"/></label>
        <label>功能<input value={filters.function_name} onChange={event => setFilter('function_name', event.target.value)} placeholder="筛选 function"/></label>
        <label>用量质量<select value={filters.quality} onChange={event => setFilter('quality', event.target.value)}><option value="">全部质量</option><option value="measured">实测</option><option value="estimated">估算</option><option value="unknown">未知</option></select></label>
      </div>
    </section>

    {error && <div className="us-error" role="alert">{error}</div>}
    <div className="us-range-note">统计周期：{summary?.range?.start || start} 至 {summary?.range?.end || end} · 时区：{summary?.timezone || '—'} · 环境：{summary?.environment || 'local'} · {summary?.history?.message || '仅汇总本机当前数据实例中已记录的事件'} {loading && <span>正在更新…</span>}</div>

    <section className="us-metric-grid" aria-label="用量摘要">
      <article className="us-metric"><small>模型 / MCP 调用</small><b>{number(totals.model_calls)} <i>/</i> {number(totals.mcp_calls)}</b><span><Activity size={13}/> 模型调用 / MCP function 调用</span></article>
      <article className="us-metric"><small>实测输入 Token</small><b>{shortNumber(totals.input_tokens)}</b><span>{tokenText(totals.input_tokens)}</span></article>
      <article className="us-metric"><small>实测输出 Token</small><b>{shortNumber(totals.output_tokens)}</b><span>{tokenText(totals.output_tokens)}</span></article>
      <article className="us-metric"><small>服务商上下文缓存 Token</small><b>{shortNumber(totals.cache_hit_tokens)} <i>/</i> {shortNumber(totals.cache_miss_tokens)}</b><span>命中 / 未命中 · 新建 {tokenText(totals.cache_creation_tokens)}</span></article>
      <article className="us-metric"><small>估算 Token（单独统计）</small><b>{shortNumber(totals.estimated_input_tokens)} <i>/</i> {shortNumber(totals.estimated_output_tokens)}</b><span>估算输入 / 输出；不计入实测 Token</span></article>
      <article className="us-metric"><small>工作台本地结果缓存</small><b>{number(totals.local_result_cache_hits)} <i>/</i> {number(totals.local_result_cache_misses)}</b><span>命中 / 未命中；与服务商上下文缓存分开</span></article>
      <article className="us-metric"><small>Reasonix Agent 回合</small><b>{number(totals.agent_turns)}</b><span>记录交互回合；内部 API Token 未知</span></article>
      <article className="us-metric"><small>模型用量记录质量</small><b>{number(Number(totals.measured_calls || 0) + Number(totals.estimated_calls || 0) + Number(totals.unknown_calls || 0))}</b><span><em className="us-quality measured">实测 {number(totals.measured_calls)}</em><em className="us-quality estimated">估算 {number(totals.estimated_calls)}</em><em className="us-quality unknown">未知 {number(totals.unknown_calls)}</em></span></article>
      <article className="us-metric"><small>MCP 结果</small><b>{number(totals.mcp_successes)} <i>/</i> {number(totals.mcp_failures)}</b><span>成功 / 失败</span></article>
    </section>

    <section className="us-panel us-trend-panel">
      <div className="us-panel-heading"><div><small>ACTIVITY TREND</small><h2>调用趋势</h2></div><div className="us-legend"><span><i className="model"/>模型调用</span><span><i className="mcp"/>MCP 调用</span></div></div>
      {series.length ? <div className="us-chart" role="img" aria-label="模型和 MCP 调用趋势">
        {series.map(item => { const model = Number(item.model_calls || 0); const mcp = Number(item.mcp_calls || 0); const modelHeight = Math.max(model ? 3 : 0, model / maxSeries * 100); const mcpHeight = Math.max(mcp ? 3 : 0, mcp / maxSeries * 100); return <div className="us-chart-column" key={item.period} title={`${item.period}: 模型 ${number(model)}，MCP ${number(mcp)}，输入 ${number(item.input_tokens)} tokens，输出 ${number(item.output_tokens)} tokens`}><div className="us-chart-bars"><i className="model" style={{ height: `${modelHeight}%` }}/><i className="mcp" style={{ height: `${mcpHeight}%` }}/></div><small>{item.period}</small></div>; })}
      </div> : <div className="us-empty">{loading ? '读取趋势中…' : '当前筛选没有趋势数据'}</div>}
      <div className="us-trend-foot">图表按调用次数展示；每个柱形悬停可查看该周期的 Token 数。</div>
    </section>

    <div className="us-breakdown-grid">
      <section className="us-panel"><div className="us-panel-heading"><div><small>PROJECT BREAKDOWN</small><h2>项目汇总</h2></div><span>{summary?.by_project?.length || 0} 个项目</span></div>
        {summary?.by_project?.length ? <div className="us-table-scroll"><table><thead><tr><th>项目</th><th>模型</th><th>MCP</th><th>Agent 回合</th><th>实测输入 / 输出</th><th>估算输入 / 输出</th><th>本地缓存命中 / 未命中</th></tr></thead><tbody>{summary.by_project.map(row => <tr key={row.workspace_id || row.workspace_name}><td><button className="us-project-link" type="button" onClick={() => { onProjectSelect(row.workspace_id); setFilter('workspace_id', String(row.workspace_id || '')); }}>{row.workspace_name || row.workspace_id || '未归属'}</button></td><td>{number(row.model_calls)}</td><td>{number(row.mcp_calls)}</td><td>{number(row.agent_turns)}</td><td>{shortNumber(row.input_tokens)} / {shortNumber(row.output_tokens)}</td><td>{shortNumber(row.estimated_input_tokens)} / {shortNumber(row.estimated_output_tokens)}</td><td>{number(row.local_result_cache_hits)} / {number(row.local_result_cache_misses)}</td></tr>)}</tbody></table></div> : <div className="us-empty">没有项目汇总数据</div>}
        {selectedProject && <div className="us-selected-project">当前项目筛选：{selectedProject.name || selectedProject.workspace_name}</div>}
      </section>
      <section className="us-panel"><div className="us-panel-heading"><div><small>TASK BREAKDOWN</small><h2>任务汇总</h2></div><span>{summary?.by_task?.length || 0} 条任务</span></div>
        {summary?.by_task?.length ? <div className="us-table-scroll"><table><thead><tr><th>模块 / 任务</th><th>项目归属</th><th>模型</th><th>MCP</th><th>Agent 回合</th><th>实测输入 / 输出</th><th>估算输入 / 输出</th><th>本地缓存命中 / 未命中</th><th>未知用量</th></tr></thead><tbody>{summary.by_task.map(row => { const key = `${row.module || ''}:${row.task_id || ''}`; const projectId = draftProjects[key] ?? row.workspace_id ?? ''; return <tr key={key}><td><button className="us-project-link" type="button" onClick={() => { setFilter('module', row.module || ''); setFilter('task_id', row.task_id || ''); }}>{row.title || row.task_id || '—'}</button><small className="us-subline">{row.module || '—'} · {row.task_id || '无任务 ID'}</small></td><td><div className="us-project-editor"><select aria-label={`${row.title || row.task_id} 项目归属`} value={projectId} onChange={event => setDraftProjects(current => ({ ...current, [key]: event.target.value }))}><option value="">未归属</option>{projectOptions.map(project => <option value={project.id} key={project.id}>{project.name}</option>)}</select><button type="button" onClick={() => saveTaskProject(row)} disabled={savingTask === key || String(projectId) === String(row.workspace_id || '')}>{savingTask === key ? '保存中' : '保存'}</button></div></td><td>{number(row.model_calls)}</td><td>{number(row.mcp_calls)}</td><td>{number(row.agent_turns)}</td><td>{shortNumber(row.input_tokens)} / {shortNumber(row.output_tokens)}</td><td>{shortNumber(row.estimated_input_tokens)} / {shortNumber(row.estimated_output_tokens)}</td><td>{number(row.local_result_cache_hits)} / {number(row.local_result_cache_misses)}</td><td>{Number(row.unknown_calls || 0) > 0 ? <em className="us-quality unknown">未知 {number(row.unknown_calls)}</em> : '0'}</td></tr>; })}</tbody></table></div> : <div className="us-empty">没有任务汇总数据</div>}
      </section>
    </div>

    <div className="us-breakdown-grid us-breakdown-grid-secondary">
      <section className="us-panel"><div className="us-panel-heading"><div><small>MODEL BREAKDOWN</small><h2>模型汇总</h2></div><span>{summary?.by_model?.length || 0} 个模型</span></div>
        {summary?.by_model?.length ? <div className="us-table-scroll"><table><thead><tr><th>服务商 / 模型</th><th>调用</th><th>实测输入 / 输出</th><th>估算输入 / 输出</th><th>未知用量</th></tr></thead><tbody>{summary.by_model.map(row => <tr key={`${row.provider}:${row.model}`}><td><button className="us-project-link" type="button" onClick={() => { setFilter('provider', row.provider === '未知' ? '' : row.provider); setFilter('model', row.model === '未知' ? '' : row.model); }}>{row.provider} · {row.model}</button></td><td>{number(row.model_calls)}</td><td>{shortNumber(row.input_tokens)} / {shortNumber(row.output_tokens)}</td><td>{shortNumber(row.estimated_input_tokens)} / {shortNumber(row.estimated_output_tokens)}</td><td>{number(row.unknown_calls)}</td></tr>)}</tbody></table></div> : <div className="us-empty">没有模型汇总数据</div>}
      </section>
      <section className="us-panel"><div className="us-panel-heading"><div><small>MCP FUNCTION BREAKDOWN</small><h2>MCP 功能汇总</h2></div><span>{summary?.by_function?.length || 0} 项功能</span></div>
        {summary?.by_function?.length ? <div className="us-table-scroll"><table><thead><tr><th>服务 / function</th><th>调用</th><th>成功</th><th>失败</th></tr></thead><tbody>{summary.by_function.map(row => <tr key={`${row.server_name}:${row.function_name}`}><td><button className="us-project-link" type="button" onClick={() => setFilter('function_name', row.function_name)}>{row.server_name} · {row.function_name}</button></td><td>{number(row.mcp_calls)}</td><td>{number(row.successes)}</td><td>{number(row.failures)}</td></tr>)}</tbody></table></div> : <div className="us-empty">没有 MCP 功能汇总数据</div>}
      </section>
    </div>

    <section className="us-panel us-events-panel"><div className="us-panel-heading"><div><small>EVENT LOG</small><h2>调用事件</h2></div><span>{number(events.total)} 条 · 每页 {PAGE_SIZE} 条</span></div>
      {events.items?.length ? <><div className="us-table-scroll"><table className="us-events-table"><thead><tr><th>详情</th><th>时间</th><th>类型 / 模块</th><th>任务</th><th>项目</th><th>服务商 / 模型</th><th>功能</th><th>状态</th><th>用量质量</th><th>输入 / 输出</th></tr></thead><tbody>{events.items.map(item => <React.Fragment key={item.id}><tr className="us-event-row"><td><button className="us-expand" type="button" aria-label={expanded === item.id ? '收起事件详情' : '展开事件详情'} onClick={() => setExpanded(expanded === item.id ? '' : item.id)}>{expanded === item.id ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}</button></td><td>{eventDateText(item.occurred_at, summary?.timezone)}</td><td><b>{eventKind(item.kind)}</b><small className="us-subline">{item.module || '—'}</small></td><td>{item.task_title || item.task_id || '—'}{item.task_title && item.task_id && <small className="us-subline">{item.task_id}</small>}</td><td>{item.project_name || '未归属'}</td><td>{[item.provider, item.model].filter(Boolean).join(' · ') || '—'}</td><td>{item.function_name || '—'}</td><td>{eventStatus(item.status)}{item.attempt ? <small className="us-subline">第 {item.attempt} 次</small> : null}</td><td><em className={`us-quality ${item.usage_quality || 'unknown'}`}>{eventQuality(item.usage_quality)}</em></td><td>{eventTokens(item.input_tokens)} / {eventTokens(item.output_tokens)}</td></tr>{expanded === item.id && <tr className="us-event-detail"><td colSpan="10"><div><span>估算输入：{tokenText(item.estimated_input_tokens)}</span><span>估算输出：{tokenText(item.estimated_output_tokens)}</span><span>服务商缓存命中：{tokenText(item.cache_hit_tokens)}</span><span>服务商缓存未命中：{tokenText(item.cache_miss_tokens)}</span><span>服务商创建缓存：{tokenText(item.cache_creation_tokens)}</span><span>工作台本地缓存：{item.kind === 'local_cache' ? eventStatus(item.status) : '—'}</span><span>耗时：{item.duration_ms == null ? '—' : `${number(item.duration_ms)} ms`}</span><span>用量来源：{item.usage_source || '—'}</span><span>记录质量：{eventQuality(item.usage_quality)}</span>{item.metadata?.image_tokens_unknown && <span>图像输入 Token：未知</span>}{item.metadata?.stage && <span>阶段：{item.metadata.stage}</span>}</div></td></tr>}</React.Fragment>)}</tbody></table></div><div className="us-pagination"><span>显示 {offset + 1}–{Math.min(offset + PAGE_SIZE, events.total)} / {number(events.total)}</span><div><button type="button" disabled={offset <= 0 || loading} onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}>上一页</button><button type="button" disabled={offset + PAGE_SIZE >= events.total || loading} onClick={() => setOffset(value => value + PAGE_SIZE)}>下一页</button></div></div></> : <div className="us-empty">{loading ? '读取事件中…' : '当前筛选没有调用事件'}</div>}
    </section>
    <footer className="us-footnote"><span>{summary?.history?.message || '历史用量缺口保持未知，不按零补齐。'}</span>{(summary?.history?.limitations || []).map(item => <span key={item}>{item}</span>)}<span>估算用量单独标记；服务商上下文缓存与工作台本地结果缓存分开统计。</span></footer>
  </main>;
}
