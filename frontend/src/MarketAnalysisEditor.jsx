import React, { useState } from 'react';
import { addMarketItem, contextReadingGroups, legacyGroups, removeMarketItem, unlinkMarketItem, updateMarketItem } from './marketAnalysis';
import { addOverviewItem, createOverviewModule, removeOverviewItem, removeOverviewModule } from './productArchiveOverview';
import MarketSegmentCards from './MarketSegmentCards.jsx';

const collections = {
  product_forms: { label: '商品形态', singular: '商品形态', fields: [['name', '名称'], ['description', '商品描述'], ['limits', '适用边界']] },
  demand_contexts: { label: '需求情境', singular: '需求情境', fields: [['name', '情境名称'], ['audience', '适用人群'], ['trigger', '触发原因'], ['task', '要完成的任务'], ['obstacles', '遇到的阻碍'], ['motivation', '背后动机'], ['desired_outcome', '期望结果'], ['product_response', '商品如何回应'], ['conditions', '使用条件'], ['boundary', '情境边界'], ['basis_note', '判断依据说明'], ['limits', '限制']] },
  cross_cutting: { label: '跨形态问题与条件', singular: '发现', fields: [['kind', '类型（适配／文化／环境／使用时段／季节／其他）'], ['title', '标题'], ['text', '说明'], ['basis', '结论性质'], ['basis_note', '判断依据说明'], ['limits', '限制']] },
  needs_validation: { label: '待验证事项', singular: '事项', fields: [['text', '待验证内容']] },
};

function Text({ label, value, onChange, multiline = false, readOnly = false }) {
  const Tag = multiline ? 'textarea' : 'input';
  return <label className={`ma-field${readOnly ? ' is-readonly' : ''}`}><span>{label}</span>{readOnly ? <div className="ma-read-value">{value || '未提供'}</div> : <Tag value={value || ''} disabled={readOnly} onChange={event => onChange(event.target.value)} rows={multiline ? 3 : undefined} />}</label>;
}

function ClaimClass({ label = '结论性质', value, onChange, readOnly = false }) {
  const names = { supported: '档案资料有线索', hypothesis: '模型推断 · 待验证' };
  return <label className="ma-field"><span>{label}</span>{readOnly ? <div className="ma-read-value">{names[value] || '未提供'}</div> : <select value={value || ''} onChange={event => onChange(event.target.value)}><option value="">未标注</option><option value="supported">档案资料有线索</option><option value="hypothesis">模型推断 · 待验证</option></select>}</label>;
}

function Basis({ value, onChange, readOnly = false }) {
  const names = { observation: '直接观察', synthesis: '资料综合', inference: '推断' };
  return <label className="ma-field"><span>判断依据类型</span>{readOnly ? <div className="ma-read-value">{names[value] || '未提供'}</div> : <select value={value || 'inference'} onChange={event => onChange(event.target.value)}><option value="observation">直接观察</option><option value="synthesis">资料综合</option><option value="inference">推断</option></select>}</label>;
}

function ContextReadingSummary({ context }) {
  const groups = contextReadingGroups(context);
  return <div className="ma-context-reading">
    {groups.map(group => <section className="ma-context-reading-group" key={group.title}>
      <h4>{group.title}</h4>
      <div>{group.items.map(item => <p key={item.key}><b>{item.label}</b><span>{item.value}</span></p>)}</div>
    </section>)}
    {context.basis_note && <p className="ma-context-basis-note"><b>判断依据说明</b><span>{context.basis_note}</span></p>}
    {context.classification && <ClaimClass readOnly value={context.classification} />}
    {context.basis && <Basis readOnly value={context.basis} />}
  </div>;
}

function AssociationSummary({ title, entries, selectedIds = [], emptyLabel }) {
  const selected = entries.filter(entry => selectedIds.includes(entry.id));
  return <div className="ma-association-summary" aria-label={title}>
    <b>{title}</b>
    {selected.length ? <ul>{selected.map(entry => <li key={entry.id}>{entry.name || '未命名'}</li>)}</ul> : <span>{emptyLabel}</span>}
  </div>;
}

function Evidence({ item, onUpdate, readOnly = false, sourceManifest = [] }) {
  const refs = Array.isArray(item.evidence_refs) ? item.evidence_refs : [];
  const sources = new Map(sourceManifest.map(source => [source.id || source.source_id, source]));
  return <fieldset className="ma-evidence" disabled={readOnly}><legend>证据来源</legend>{refs.map((ref, index) => <div className="ma-evidence-row" key={`${typeof ref === 'string' ? ref : ref.id || ref.url || index}`}>
    <p>{typeof ref === 'string' ? (sources.get(ref)?.name || sources.get(ref)?.title || ref) : (ref.title || ref.label || ref.source_id || ref.url || '未标注来源')}{typeof ref === 'string' && <small>来源编号：{ref}</small>}{typeof ref !== 'string' && ref.url && <a href={ref.url} target="_blank" rel="noreferrer">查看来源</a>}</p>
    {!readOnly && <button type="button" aria-label={`删除来源 ${index + 1}`} onClick={() => onUpdate({ evidence_refs: refs.filter((_, i) => i !== index) })}>移除</button>}
  </div>)}</fieldset>;
}

function LegacyArchive({ modules, onChange }) {
  const [editing, setEditing] = useState(false);
  const [newModuleTitle, setNewModuleTitle] = useState('');
  const keywordModule = modules.find(module => module.field_key === 'workflow_keywords');
  const archiveModules = modules.filter(module => module.field_key !== 'workflow_keywords');
  const groups = legacyGroups(archiveModules);
  const segmented = new Map();
  const unsegmented = [];
  for (const module of archiveModules) for (const item of module.items || []) {
    if (item.segment_id) {
      const key = item.segment_id;
      if (!segmented.has(key)) segmented.set(key, { id: key, name: item.segment_name || key, entries: [] });
      segmented.get(key).entries.push({ module, item });
    } else unsegmented.push({ module, item });
  }
  return <><section className="ma-legacy" aria-labelledby="ma-legacy-title">
    <div className="ma-legacy-head"><div><span>历史档案</span><h3 id="ma-legacy-title">按原有信息保留</h3></div><p>此记录没有商品形态结构；不会从旧文字推测形态或关联。</p></div>
    <p className="ma-legacy-note"><strong>商品形态：旧档案未记录。</strong> 以下仅按原有细分编号归拢内容。</p>
    {[...segmented.values()].map(segment => <details className="ma-legacy-context" key={segment.id}>
      <summary><b>{segment.name}</b><span>细分编号 {segment.id}</span><span>{segment.entries.length} 条历史内容</span></summary>
      <div>{segment.entries.map(({ module, item }) => <article key={`${module.id}-${item.id}`}><small>{module.title}</small><p>{item.text}</p>{item.basis_note && <small>依据：{item.basis_note}</small>}{item.limits && <small>边界：{item.limits}</small>}{(item.links || []).map((link, index) => { const href = typeof link === 'string' ? link : link.url || link.href; return href ? <a key={`${href}-${index}`} href={href} target="_blank" rel="noreferrer">查看证据：{typeof link === 'string' ? href : link.title || link.label || href}</a> : null; })}</article>)}</div>
    </details>)}
    {!!unsegmented.length && <details className="ma-legacy-custom"><summary>自定义或未关联内容（{unsegmented.length}）</summary><div>{unsegmented.map(({ module, item }) => <article key={`${module.id}-${item.id}`}><b>{module.title}</b><p>{item.text}</p></article>)}</div></details>}
    <details className="ma-legacy-editor"><summary>查看原有模块（保留完整字段）</summary><div><button type="button" onClick={() => setEditing(value => !value)}>{editing ? '结束编辑' : '编辑旧档案内容'}</button>{editing && <form className="ma-legacy-add-module" onSubmit={event => { event.preventDefault(); const title = newModuleTitle.trim(); if (!title) return; onChange(items => [...items, createOverviewModule(title)]); setNewModuleTitle(''); }}><label>自定义模块名称<input value={newModuleTitle} onChange={event => setNewModuleTitle(event.target.value)} placeholder="例如：购买顾虑" /></label><button type="submit" disabled={!newModuleTitle.trim()}>新增自定义模块</button></form>}{groups.map(group => <section key={group.title}><h4>{group.title}</h4>{group.modules.map(module => <article key={module.id}><header className="ma-legacy-module-head"><label>模块名称<input value={module.title || ''} disabled={!editing || module.read_only} onChange={event => onChange(items => items.map(value => value.id === module.id ? { ...value, title: event.target.value } : value))} /></label>{editing && !module.read_only && !module.field_key && <button className="ma-danger-button" type="button" aria-label={`删除自定义模块 ${module.title || '未命名'}`} onClick={() => { if (window.confirm(`删除自定义模块“${module.title || '未命名'}”及其全部内容？`)) onChange(items => removeOverviewModule(items, module.id)); }}>删除自定义模块</button>}</header>{(module.items || []).map(item => <div className="ma-legacy-item-editor" key={item.id}><label>历史内容<textarea value={item.text || ''} readOnly={!editing || module.read_only} onChange={event => onChange(items => items.map(value => value.id === module.id ? { ...value, items: value.items.map(existing => existing.id === item.id ? { ...existing, text: event.target.value } : existing) } : value))} /></label>{editing && !module.read_only && <button className="ma-danger-button" type="button" aria-label={`删除${module.title || '模块'}中的历史内容`} onClick={() => { if (window.confirm('删除这条历史内容？')) onChange(items => removeOverviewItem(items, module.id, item.id)); }}>删除内容</button>}</div>)}{editing && !module.read_only && <button type="button" onClick={() => onChange(items => addOverviewItem(items, module.id))}>＋ 新增一条内容</button>}</article>)}</section>)}</div></details>
  </section>{keywordModule && <section className="ma-keywords" aria-label="业务关键词只读结果"><small>关键词任务结果 · 只读</small><h3>业务关键词</h3>{keywordModule.items?.length ? <ul>{keywordModule.items.map(item => <li key={item.id}>{item.text}</li>)}</ul> : <p>暂无已完成关键词任务结果。</p>}</section>}</>;
}

export default function MarketAnalysisEditor({ value, onChange, legacyModules = [], setLegacyModules, readOnly = false, legacyOnly = false, sourceManifest = [], seasonalityChart = null }) {
  if (value?.schema_version === 3 && !legacyOnly) {
    const keywordModule = legacyModules.find(module => module.field_key === 'workflow_keywords');
    const historicalModules = legacyModules.filter(module => module.field_key !== 'workflow_keywords');
    return <>
      <MarketSegmentCards value={value} onChange={onChange} readOnly={readOnly} sourceManifest={sourceManifest} seasonalityChart={seasonalityChart} />
      {keywordModule && <section className="ma-keywords" aria-label="业务关键词只读结果"><small>仅来自已完成的关键词任务 · 只读</small><h3>业务关键词</h3>{keywordModule.items?.length ? <ul>{keywordModule.items.map(item => <li key={item.id}>{item.text}</li>)}</ul> : <p>暂无已完成关键词任务结果。</p>}</section>}
      {historicalModules.length > 0 && setLegacyModules && <details className="ma-v3-history"><summary>查看旧档案原文（{historicalModules.reduce((sum, module) => sum + (module.items || []).length, 0)} 条）</summary><LegacyArchive modules={historicalModules} onChange={setLegacyModules} /></details>}
    </>;
  }
  const analysis = value || { schema_version: 2, overview: {}, product_forms: [], demand_contexts: [], cross_cutting: [], needs_validation: [] };
  const changeOverview = (key, val) => onChange({ ...analysis, overview: { ...(analysis.overview || {}), [key]: val } });
  const changeItem = (collection, itemId, patch) => onChange(updateMarketItem(analysis, collection, itemId, patch));
  const forms = analysis.product_forms || [];
  const contexts = analysis.demand_contexts || [];
  return <div className={`ma-editor${readOnly ? ' is-readonly' : ''}`}>
    {!legacyOnly && <>
    <section className="ma-overview"><div><span>品类总览</span><h3>先看整体，再看不同形态与需求</h3></div><Text readOnly={readOnly} label="品类总述" multiline value={analysis.overview?.text} onChange={v => changeOverview('text', v)} /><div className="ma-meta-grid"><ClaimClass readOnly={readOnly} value={analysis.overview?.classification} onChange={v => changeOverview('classification', v)} /><Text readOnly={readOnly} label="判断依据" value={analysis.overview?.basis_note} onChange={v => changeOverview('basis_note', v)} /><Text readOnly={readOnly} label="适用限制" value={analysis.overview?.limits} onChange={v => changeOverview('limits', v)} /></div><Evidence readOnly={readOnly} sourceManifest={sourceManifest} item={analysis.overview || {}} onUpdate={patch => onChange({ ...analysis, overview: { ...(analysis.overview || {}), ...patch } })} /></section>
    <section className="ma-forms"><header><div><small>01 · 商品形态</small><h3>这份资料里有哪些不同商品</h3></div>{!readOnly && <button type="button" onClick={() => onChange(addMarketItem(analysis, 'product_forms', { name: '', description: '', limits: '' }))}>＋ 添加形态</button>}</header>{forms.length ? forms.map((form, index) => <article className="ma-form" key={form.id}><header><span>{String(index + 1).padStart(2, '0')}</span><b>{form.name || '未命名商品形态'}</b>{!readOnly && <button type="button" aria-label={`删除商品形态 ${form.name || index + 1}`} onClick={() => onChange({ ...unlinkMarketItem(analysis, 'product_forms', form.id), product_forms: forms.filter(item => item.id !== form.id) })}>删除</button>}</header><Text label="形态名称" value={form.name} onChange={v => changeItem('product_forms', form.id, { name: v })} readOnly={readOnly} /><Text label="商品完整描述" multiline value={form.description} onChange={v => changeItem('product_forms', form.id, { description: v })} readOnly={readOnly} /><div className="ma-meta-grid"><ClaimClass readOnly={readOnly} value={form.classification} onChange={v => changeItem('product_forms', form.id, { classification: v })} /><Basis readOnly={readOnly} value={form.basis} onChange={v => changeItem('product_forms', form.id, { basis: v })} /><Text label="判断依据说明" value={form.basis_note} onChange={v => changeItem('product_forms', form.id, { basis_note: v })} readOnly={readOnly} /><Text label="适用边界" value={form.limits} onChange={v => changeItem('product_forms', form.id, { limits: v })} readOnly={readOnly} /></div><Evidence readOnly={readOnly} sourceManifest={sourceManifest} item={form} onUpdate={patch => changeItem('product_forms', form.id, patch)} /></article>) : <p className="ma-empty">当前分析没有提取到可区分的商品形态。</p>}</section>
    <section className="ma-contexts"><header><div><small>02 · 需求情境</small><h3>谁在什么条件下，想完成什么</h3><p>一个情境可关联多种商品形态；同一种形态也可服务多个情境。</p></div>{!readOnly && <button type="button" onClick={() => onChange(addMarketItem(analysis, 'demand_contexts', { name: '', product_form_ids: [] }))}>＋ 添加情境</button>}</header>{contexts.length ? contexts.map((context, index) => <article className="ma-context" key={context.id}><header><span>{String(index + 1).padStart(2, '0')}</span><b>{context.name || '未命名需求情境'}</b>{!readOnly && <button type="button" aria-label={`删除需求情境 ${context.name || index + 1}`} onClick={() => onChange({ ...unlinkMarketItem(analysis, 'demand_contexts', context.id), demand_contexts: contexts.filter(item => item.id !== context.id) })}>删除</button>}</header>{readOnly ? <ContextReadingSummary context={context} /> : <div className="ma-context-fields">{collections.demand_contexts.fields.map(([key, label]) => <Text key={key} label={label} multiline={['obstacles', 'product_response', 'conditions', 'boundary', 'basis_note', 'limits'].includes(key)} value={context[key]} onChange={v => changeItem('demand_contexts', context.id, { [key]: v })} />)}<ClaimClass value={context.classification} onChange={v => changeItem('demand_contexts', context.id, { classification: v })} /><Basis value={context.basis} onChange={v => changeItem('demand_contexts', context.id, { basis: v })} /></div>}{readOnly ? <AssociationSummary title="关联商品形态" entries={forms} selectedIds={context.product_form_ids || []} emptyLabel="未归类" /> : <fieldset className="ma-associations"><legend>关联商品形态</legend>{forms.length ? <>{forms.map(form => <label key={form.id}><input type="checkbox" checked={(context.product_form_ids || []).includes(form.id)} onChange={event => changeItem('demand_contexts', context.id, { product_form_ids: event.target.checked ? [...new Set([...(context.product_form_ids || []), form.id])] : (context.product_form_ids || []).filter(id => id !== form.id) })} />{form.name || '未命名商品形态'}</label>)}{!(context.product_form_ids || []).length && <p>未归类到具体商品形态</p>}</> : <p>尚无已识别商品形态；可以先保留情境，稍后再关联。</p>}</fieldset>}<Evidence readOnly={readOnly} sourceManifest={sourceManifest} item={context} onUpdate={patch => changeItem('demand_contexts', context.id, patch)} /></article>) : <p className="ma-empty">当前分析没有提取到需求情境。</p>}</section>
    <section className="ma-cross"><header><div><small>03 · 横跨多个形态的发现</small><h3>适配、文化、环境与时间条件</h3><p>只记录确实影响结论的发现；不用每个主题都填一段。</p></div>{!readOnly && <button type="button" onClick={() => onChange(addMarketItem(analysis, 'cross_cutting', { kind: '', title: '', text: '', product_form_ids: [], context_ids: [] }))}>＋ 添加发现</button>}</header>{(analysis.cross_cutting || []).map(item => <article className="ma-cross-item" key={item.id}>{collections.cross_cutting.fields.filter(([key]) => key !== 'basis').map(([key, label]) => <Text key={key} label={label} multiline={['text', 'basis_note', 'limits'].includes(key)} value={item[key]} onChange={v => changeItem('cross_cutting', item.id, { [key]: v })} readOnly={readOnly} />)}<ClaimClass readOnly={readOnly} value={item.classification} onChange={v => changeItem('cross_cutting', item.id, { classification: v })} /><Basis readOnly={readOnly} value={item.basis} onChange={v => changeItem('cross_cutting', item.id, { basis: v })} />{readOnly ? <AssociationSummary title="关联商品形态与情境" entries={[...forms.map(entry => ({ ...entry, name: `形态 · ${entry.name || '未命名'}` })), ...contexts.map(entry => ({ ...entry, name: `情境 · ${entry.name || '未命名'}` }))]} selectedIds={[...(item.product_form_ids || []), ...(item.context_ids || [])]} emptyLabel="未关联形态或情境" /> : <fieldset className="ma-associations"><legend>关联商品形态与情境</legend>{[...forms.map(x => ['product_form_ids', x]), ...contexts.map(x => ['context_ids', x])].map(([field, entry]) => <label key={`${field}-${entry.id}`}><input type="checkbox" checked={(item[field] || []).includes(entry.id)} onChange={event => changeItem('cross_cutting', item.id, { [field]: event.target.checked ? [...new Set([...(item[field] || []), entry.id])] : (item[field] || []).filter(id => id !== entry.id) })} />关联{entry.name || '未命名'}</label>)}{!(item.product_form_ids || []).length && !(item.context_ids || []).length && <p>尚未关联形态或情境</p>}</fieldset>}{/(季节|旺季|销售峰值)/.test(`${item.kind||''} ${item.title||''}`) && <p className="ma-season-note">使用时段可以是推断；实际销售旺季需要至少三个可比月份的资料来源。</p>}<Evidence readOnly={readOnly} sourceManifest={sourceManifest} item={item} onUpdate={patch => changeItem('cross_cutting', item.id, patch)} />{!readOnly && <button type="button" onClick={() => onChange(removeMarketItem(analysis, 'cross_cutting', item.id))}>删除发现</button>}</article>)}</section>
    <section className="ma-validation"><header><div><small>04 · 待验证</small><h3>哪些判断还需要更多资料</h3></div>{!readOnly && <button type="button" onClick={() => onChange(addMarketItem(analysis, 'needs_validation', { text: '', product_form_ids: [], context_ids: [] }))}>＋ 添加待验证</button>}</header>{(analysis.needs_validation || []).map(item => <article key={item.id}><Text label="待验证事项" multiline value={item.text} onChange={v => changeItem('needs_validation', item.id, { text: v })} readOnly={readOnly} />{!readOnly && <button type="button" onClick={() => onChange(removeMarketItem(analysis, 'needs_validation', item.id))}>移除</button>}</article>)}</section>
    </>}
    {legacyModules.length > 0 && setLegacyModules && <LegacyArchive modules={legacyModules} onChange={setLegacyModules} />}
  </div>;
}
