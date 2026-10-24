import React from 'react';
import { editSegmentClaim, emptySegment, moveSegment, newSegmentClaim, overviewReadingParagraphs, overviewReadingParts, SEGMENT_FIELDS, updateSegment } from './marketSegmentCards.js';

const KeywordSeasonalityPanel = React.lazy(() => import('./KeywordSeasonalityPanel.jsx'));

const metaName = { observation: '资料观察', synthesis: '资料综合', inference: '推断' };

function ClaimSources({ claim, sources, showContext = true }) {
  const refs = claim.evidence_refs || [];
  return <>
    <span>{metaName[claim.basis] || '未标注'}{claim.classification === 'hypothesis' ? ' · 待核实' : ''}</span>
    {showContext && claim.basis_note && <span>依据：{claim.basis_note}</span>}
    {showContext && claim.limits && <span>边界：{claim.limits}</span>}
    {refs.length > 0 && <span>来源：{refs.map((ref, i) => {
      const source = sources.find(item => item.id === ref || item.source_id === ref || item.evidence_id === ref);
      const label = source?.name || source?.title || ref;
      const url = source?.url || source?.source_url;
      return <React.Fragment key={`${ref}-${i}`}>{i > 0 ? '、' : ''}{/^https:\/\//i.test(url || '') ? <a href={url} target="_blank" rel="noreferrer">{label}</a> : label}</React.Fragment>;
    })}</span>}
  </>;
}

function SegmentField({ segment, field, label, readOnly, onChange, sources }) {
  const group = segment.fields?.[field] || { items: [], missing_reason: '未形成结论。' };
  const items = group.items || [];
  const changeGroup = next => onChange({ ...segment, fields: { ...segment.fields, [field]: next } });
  return <div className="ma-v3-field">
    <h4>{label}</h4>
    {readOnly ? <div className="ma-v3-lines">{items.length ? items.map((claim, i) => <p key={i}>{claim.text}{field === 'seasonality' && <small>{claim.kind === 'sales_peak' ? '实测销量旺季' : claim.kind === 'search_demand_peak' ? '搜索需求高点' : '可能使用时段'}</small>}</p>) : <p className="ma-v3-missing">{group.missing_reason || '现有资料未形成可靠结论。'}</p>}</div>
      : <div className="ma-v3-edit-lines">{items.map((claim, i) => <div className="ma-v3-edit-line" key={i}>
        <label><span>{label} {i + 1}</span><textarea rows={2} value={claim.text || ''} onChange={event => changeGroup({ ...group, items: items.map((item, index) => index === i ? editSegmentClaim(item, event.target.value) : item) })} /></label>
        <button type="button" aria-label={`删除${label}第${i + 1}条`} onClick={() => changeGroup({ ...group, items: items.filter((_, index) => index !== i), missing_reason: group.missing_reason || '待补充。' })}>删除</button>
      </div>)}{items.length < 6 && <button type="button" onClick={() => changeGroup({ ...group, items: [...items, newSegmentClaim(field)], missing_reason: '' })}>＋ 添加{label}</button>}
        {!items.length && <label className="ma-v3-gap-edit"><span>暂无结论的原因</span><input value={group.missing_reason || ''} onChange={event => changeGroup({ ...group, missing_reason: event.target.value })} /></label>}
      </div>}
  </div>;
}

function ValidationEditor({ items, onChange }) {
  return <div className="ma-v3-validation-edit"><b>待验证事项</b>{items.map((item, i) => <div key={item.id || i}><input aria-label={`待验证事项 ${i + 1}`} value={item.text || ''} onChange={event => onChange(items.map((value, index) => index === i ? { ...value, text: event.target.value } : value))} /><button type="button" onClick={() => onChange(items.filter((_, index) => index !== i))}>删除</button></div>)}<button type="button" onClick={() => onChange([...items, { id: `V-${globalThis.crypto?.randomUUID?.() || Date.now()}`, text: '' }])}>＋ 添加待验证</button></div>;
}

export default function MarketSegmentCards({ value, onChange, readOnly = false, sourceManifest = [], seasonalityChart = null }) {
  const analysis = value;
  const segments = analysis.segments || [];
  const setSegment = (id, next) => onChange(updateSegment(analysis, id, () => next));
  const overview = analysis.overview || {};
  const { summary, boundary } = overviewReadingParts(overview.text);
  const overviewPoints = Array.isArray(overview.points) ? overview.points : [];
  const summaryParagraphs = overviewPoints.length ? [summary] : overviewReadingParagraphs(summary);
  const hasOverviewContext = readOnly && Boolean(boundary || overview.basis_note || overview.limits);
  return <div className={`ma-v3${readOnly ? ' is-readonly' : ''}`}>
    <section className={`ma-v3-overview${hasOverviewContext ? ' has-context' : ''}`}><header><small>PRODUCT / MARKET OVERVIEW</small><h3>当前榜单综述</h3><span>仅概括本档案样本，不代表整个市场</span></header>
      {readOnly ? <div className="ma-v3-overview-reading"><div className="ma-v3-overview-main">{(summaryParagraphs.length ? summaryParagraphs : ['暂无综述。']).map((paragraph, index) => <p className={index === 0 ? 'ma-v3-overview-lead' : ''} key={index}>{paragraph}</p>)}
        {overviewPoints.length > 0 && <dl className="ma-v3-overview-points">{overviewPoints.map((point, index) => <div key={index}><dt>{point.label}</dt><dd>{point.text}</dd></div>)}</dl>}</div>{hasOverviewContext && <aside className="ma-v3-overview-context" aria-label="综述判断依据与适用边界">
        {overview.basis_note && <div><h4>判断依据</h4><p>{overview.basis_note}</p></div>}
        {boundary && <div><h4>样本边界</h4><p>{boundary}</p></div>}
        {overview.limits && <div><h4>适用边界</h4><p>{overview.limits}</p></div>}
      </aside>}</div> : <div className="ma-v3-overview-edit"><label><span>核心判断</span><textarea rows={3} value={overview.text || ''} onChange={event => onChange({ ...analysis, overview: editSegmentClaim(overview, event.target.value) })} /></label>
        {overviewPoints.map((point, index) => <div className="ma-v3-overview-point-edit" key={index}><label><span>条目标题</span><input maxLength={16} value={point.label || ''} onChange={event => onChange({ ...analysis, overview: { ...overview, points: overviewPoints.map((item, i) => i === index ? { ...item, label: event.target.value } : item) } })} /></label><label><span>条目内容</span><textarea rows={2} value={point.text || ''} onChange={event => onChange({ ...analysis, overview: { ...overview, points: overviewPoints.map((item, i) => i === index ? { ...editSegmentClaim(item, event.target.value), label: item.label } : item) } })} /></label><button type="button" onClick={() => onChange({ ...analysis, overview: { ...overview, points: overviewPoints.filter((_, i) => i !== index) } })}>删除条目</button></div>)}
        {overviewPoints.length < 4 && <button type="button" onClick={() => onChange({ ...analysis, overview: { ...overview, points: [...overviewPoints, { ...newSegmentClaim('overview'), label: '补充观察' }] } })}>＋ 添加综述条目</button>}</div>}
      <details><summary>{hasOverviewContext ? '来源与引用' : '综述依据与边界'}</summary><div className="ma-v3-evidence"><ClaimSources claim={overview} sources={sourceManifest} showContext={!hasOverviewContext} />{overviewPoints.map((point, index) => <p key={index}><b>{point.label || `条目 ${index + 1}`}</b><ClaimSources claim={point} sources={sourceManifest} /></p>)}</div></details>
    </section>
    {seasonalityChart && <React.Suspense fallback={<p className="ma-trend-state" role="status">正在加载关键词走势…</p>}><KeywordSeasonalityPanel {...seasonalityChart} /></React.Suspense>}
    <div className="ma-v3-heading"><div><small>MARKET SEGMENTS</small><h3>细分市场</h3></div>{!readOnly && <button type="button" onClick={() => onChange({ ...analysis, segments: [...segments, emptySegment()] })}>＋ 添加细分</button>}</div>
    <div className="ma-v3-grid">{segments.map((segment, index) => <article className="ma-v3-card" key={segment.id}>
      <header><span>{String(index + 1).padStart(2, '0')}</span>{readOnly ? <h3>{segment.name || '未命名细分'}</h3> : <label><span>细分名称</span><input value={segment.name || ''} onChange={event => setSegment(segment.id, { ...segment, name: event.target.value })} /></label>}</header>
      {!readOnly && <div className="ma-v3-card-actions"><button type="button" aria-label={`上移${segment.name || '细分'}`} disabled={index === 0} onClick={() => onChange(moveSegment(analysis, segment.id, -1))}>上移</button><button type="button" aria-label={`下移${segment.name || '细分'}`} disabled={index === segments.length - 1} onClick={() => onChange(moveSegment(analysis, segment.id, 1))}>下移</button><button type="button" className="ma-v3-delete" onClick={() => { if (window.confirm(`删除细分“${segment.name || '未命名'}”及其内容？`)) onChange({ ...analysis, segments: segments.filter(item => item.id !== segment.id) }); }}>删除细分</button></div>}
      <div className="ma-v3-fields">{SEGMENT_FIELDS.map(([field, label]) => <SegmentField key={field} segment={segment} field={field} label={label} readOnly={readOnly} onChange={next => setSegment(segment.id, next)} sources={sourceManifest} />)}</div>
      <details className="ma-v3-card-evidence"><summary>依据与待验证</summary><div className="ma-v3-evidence">{SEGMENT_FIELDS.map(([field, label]) => (segment.fields?.[field]?.items || []).map((claim, i) => <p key={`${field}-${i}`}><b>{label}{i > 0 ? ` ${i + 1}` : ''}</b><ClaimSources claim={claim} sources={sourceManifest} /></p>))}
        {(segment.needs_validation || []).map((item, i) => <p key={item.id || i}><b>待验证</b><span>{item.text}</span></p>)}
        {!readOnly && <ValidationEditor items={segment.needs_validation || []} onChange={next => setSegment(segment.id, { ...segment, needs_validation: next })} />}
      </div></details>
    </article>)}</div>
    {(analysis.needs_validation || []).length > 0 && <details className="ma-v3-global-validation"><summary>跨细分待验证 · {analysis.needs_validation.length} 项</summary>{analysis.needs_validation.map((item, i) => <p key={item.id || i}>{item.text}</p>)}</details>}
  </div>;
}
