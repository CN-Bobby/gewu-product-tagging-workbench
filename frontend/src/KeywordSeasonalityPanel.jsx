import React, { useEffect, useMemo, useState } from 'react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { formatSearches, KEYWORD_TREND_COLORS, trendRows } from './keywordTrendChart.js';

function TrendTooltip({ active, label, payload, keywords, mode }) {
  if (!active || !payload?.length) return null;
  return <div className="ma-trend-tooltip"><b>{label} · {mode === 'index' ? '相对月均指数' : '月搜索量'}</b>
    {payload.map(item => {
      const index = Number(String(item.dataKey).slice(1));
      if (!Number.isInteger(index) || !keywords[index]) return null;
      return <p key={index}><span style={{ background: KEYWORD_TREND_COLORS[index] }} />
        <strong>{keywords[index].keyword}</strong>
        <em>{mode === 'index' ? `${item.value} · ${formatSearches(item.payload[`raw${index}`])} 次` : `${formatSearches(item.value)} 次`}</em></p>;
    })}
  </div>;
}

export default function KeywordSeasonalityPanel({ api, archiveId, sourceId = '', sourceIds = [] }) {
  const [sourceResults, setSourceResults] = useState([]);
  const [activeSource, setActiveSource] = useState(0);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [mode, setMode] = useState('index');
  const [focused, setFocused] = useState(null);
  const sourceKey = sourceIds.length ? sourceIds.join('|') : sourceId;
  useEffect(() => {
    if (!archiveId) return;
    let active = true;
    setSourceResults([]);
    setError('');
    const candidates = sourceKey ? sourceKey.split('|') : [''];
    Promise.allSettled(candidates.map(id => {
      const query = id ? `?source_id=${encodeURIComponent(id)}` : '';
      return api(`/api/product-archive/archives/${archiveId}/keyword-seasonality${query}`, undefined, 'GET');
    })).then(responses => {
      if (!active) return;
      const successful = responses.flatMap((response, index) => response.status === 'fulfilled'
        ? [{ id: candidates[index], value: response.value }] : []);
      if (!successful.length) {
        setError(responses[0]?.reason?.message || '关键词趋势读取失败');
        return;
      }
      const best = successful.reduce((winner, item, index) => {
        const score = value => value?.status === 'available' ? (value.keywords?.length || 0) : 0;
        return score(item.value) > score(successful[winner].value) ? index : winner;
      }, 0);
      setSourceResults(successful);
      setActiveSource(best);
      setFocused(null);
    });
    return () => { active = false; };
  }, [api, archiveId, sourceKey, reload]);

  const result = sourceResults[activeSource]?.value || null;
  const keywords = result?.keywords || [];
  const rows = useMemo(() => trendRows(result, mode), [result, mode]);
  const selected = focused || keywords.map((_, index) => index);
  const toggle = index => setFocused(current => {
    if (current === null) return [index];
    const next = current.includes(index) ? current.filter(item => item !== index) : [...current, index];
    return !next.length || next.length === keywords.length ? null : next;
  });
  const hasData = result?.status === 'available' && keywords.length && rows.length;
  const period = rows.length ? `${rows[0].month}—${rows[rows.length - 1].month}` : '';

  return <section className="ma-trend" aria-label="关键词搜索需求季节走势">
    <header className="ma-trend-head"><div><small>KEYWORD DEMAND / SEASONALITY</small><h3>关键词搜索需求走势</h3>
      <p>同一张图比较报告中的主关键词；部分词可能覆盖相邻品类，搜索需求不等于商品销量。</p></div>
      {hasData && <div className="ma-trend-mode" role="group" aria-label="纵轴口径">
        <button type="button" aria-pressed={mode === 'index'} onClick={() => setMode('index')}>相对走势</button>
        <button type="button" aria-pressed={mode === 'raw'} onClick={() => setMode('raw')}>原始搜索量</button>
      </div>}</header>
    {error ? <div className="ma-trend-state" role="alert">{error} <button type="button" onClick={() => setReload(value => value + 1)}>重试</button></div>
      : !result ? <p className="ma-trend-state" role="status">正在读取已完成的关键词报告…</p>
        : !hasData ? <p className="ma-trend-state" role="status">{({
          no_keyword_report: '当前档案没有已完成的关键词趋势报告。',
          trend_sheet_missing: '这份关键词报告没有 24 个月搜索量趋势表。',
          no_monthly_data: '这份报告的可用月份不足，暂不能绘制走势图。',
          read_failed: '关键词报告读取失败，请核对项目资料中的文件。',
        })[result.status] || '暂无可绘制的关键词月度数据。'}</p>
          : <>
            <div className="ma-trend-meta"><span>{result.marketplace || '—'} · {period} · {keywords.length} 个主关键词</span>
              <span title={result.source?.name || ''}>来源：{result.source?.name || '已完成关键词任务报告'}</span></div>
            {sourceResults.length > 1 && <label className="ma-trend-source">切换关键词报告
              <select value={activeSource} onChange={event => { setActiveSource(Number(event.target.value)); setFocused(null); }}>
                {sourceResults.map((item, index) => <option key={item.id || index} value={index}>
                  {item.value.source?.name || `关键词报告 ${index + 1}`} · {item.value.keywords?.length || 0} 个主关键词
                </option>)}
              </select>
            </label>}
            <p className="ma-trend-axis-note">{mode === 'index' ? '各词自身有效月份的平均搜索量 = 100；便于比较季节形状。悬停可看原始搜索量。' : '纵轴为报告记录的月搜索量；搜索量较小的词可能贴近底部。'} 实线为核心词，虚线为长尾词。</p>
            <div className="ma-trend-plot"><ResponsiveContainer width="100%" height={310}>
              <LineChart data={rows} margin={{ top: 9, right: 14, bottom: 4, left: 0 }} accessibilityLayer>
                <CartesianGrid stroke="#e6ebe4" vertical={false} />
                <XAxis dataKey="month" tickFormatter={value => value.slice(2)} interval={rows.length > 15 ? 2 : 0} tick={{ fill: '#607265', fontSize: 11 }} tickLine={false} axisLine={{ stroke: '#becfc0' }} />
                <YAxis width={58} tickFormatter={value => mode === 'index' ? String(Math.round(value)) : Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value)} tick={{ fill: '#607265', fontSize: 11 }} tickLine={false} axisLine={false} domain={[0, 'auto']} />
                <Tooltip content={<TrendTooltip keywords={keywords} mode={mode} />} />
                {keywords.map((item, index) => selected.includes(index) && <Line key={item.keyword} type="linear" dataKey={`k${index}`} name={item.keyword} stroke={KEYWORD_TREND_COLORS[index]} strokeWidth={focused ? 2.8 : 1.8} strokeDasharray={index >= 5 ? '5 3' : undefined} dot={false} activeDot={{ r: 4 }} connectNulls={false} isAnimationActive={false} />)}
              </LineChart>
            </ResponsiveContainer></div>
            <div className="ma-trend-legend" aria-label="选择要显示的关键词">{keywords.map((item, index) => <button key={`${item.keyword}-${index}`} type="button" aria-pressed={selected.includes(index)} onClick={() => toggle(index)} title={[item.keyword, item.translation, item.type].filter(Boolean).join(' · ')}>
              <span className={`ma-trend-swatch${index >= 5 ? ' is-dashed' : ''}`} style={{ borderColor: KEYWORD_TREND_COLORS[index] }} /><span className="ma-trend-term">{item.keyword}</span>
            </button>)}{focused && <button type="button" className="ma-trend-reset" onClick={() => setFocused(null)}>显示全部</button>}</div>
            <details className="ma-trend-table"><summary>查看 24 个月原始数值</summary><div><table><thead><tr><th scope="col">月份</th>{keywords.map(item => <th scope="col" key={item.keyword}>{item.keyword}</th>)}</tr></thead>
              <tbody>{rows.map(row => <tr key={row.month}><th scope="row">{row.month}</th>{keywords.map((item, index) => <td key={item.keyword}>{formatSearches(row[`raw${index}`])}</td>)}</tr>)}</tbody></table></div></details>
          </>}
  </section>;
}
