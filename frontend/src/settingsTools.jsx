import React,{useState,useEffect} from 'react';

function localModel(provider,value){
  let result=String(value||'').trim();
  const prefix=String(provider||'').trim()+'/';
  while(prefix!=='/'&&result.startsWith(prefix)) result=result.slice(prefix.length);
  return result.replace(/^\/+|\/+$/g,'');
}

function validationLabel(value){
  if(value?.status==='validated') return '已通过';
  if(value?.status==='failed') return '验证失败';
  return value?'待验证':'未验证';
}

export function ProviderSettings({api,onSaved}){
  const [name,setName]=useState('deepseek');
  const [base,setBase]=useState('');
  const [key,setKey]=useState('');
  const [credentialId,setCredentialId]=useState('');
  const [credentialName,setCredentialName]=useState('默认 Key');
  const [setDefault,setSetDefault]=useState(true);
  const [textModel,setTextModel]=useState('');
  const [visionModel,setVisionModel]=useState('');
  const [status,setStatus]=useState(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);

  const load=()=>api('/api/settings/models').then(value=>{
    setStatus(value);
    const provider=(value.providers||[]).find(p=>p.name===(value.default_model||'').split('/')[0])||(value.providers||[])[0];
    if(provider){
      setName(provider.name||'');
      setBase(provider.base_url||'');
      const models=(provider.models||[]).filter(Boolean);
      setTextModel(localModel(provider.name,provider.text_model||provider.default||models[0]||''));
      setVisionModel(localModel(provider.name,provider.vision_model||models.find(m=>/vision/i.test(m))||models[1]||models[0]||''));
      const credential=(provider.credentials||[]).find(x=>x.id===provider.primary_credential_id)||(provider.credentials||[])[0];
      setCredentialId(credential?.id||'');setCredentialName(credential?.name||'默认 Key');
    }
  });
  useEffect(()=>{load().catch(e=>setError(e.message))},[]);

  const textLocal=localModel(name,textModel),visionLocal=localModel(name,visionModel);
  const textRef=name&&textLocal?`${name}/${textLocal}`:'—';
  const visionRef=name&&visionLocal?`${name}/${visionLocal}`:'—';
  const provider=status?.providers?.find(p=>p.name===name);
  const validation=provider?.validation;
  const submit=async()=>{
    setBusy(true);setError('');
    try{
      await api('/api/settings/models',{name,base_url:base,text_model:textLocal,vision_model:visionLocal,api_key:key,credential_id:credentialId||undefined,credential_name:credentialName,set_default:setDefault});
      setKey('');
      const value=await api('/api/settings/models');
      setStatus(value);
      onSaved(value);
    }catch(e){setError(e.message)}finally{setBusy(false)}
  };
  return <section className="provider-settings">
    <div className="settings-intro">
      <div><span className="eyebrow">本机连接</span><h3>模型服务与授权</h3></div>
      <p>配置保存前会分别发送一条不含商品数据的最小文本请求和视觉请求。密钥只保存在本机。</p>
    </div>
    <div className="settings-card">
      <div className="settings-card-title"><strong>服务连接</strong><span>OpenAI 兼容接口</span></div>
      {(status?.providers||[]).length>0&&<label className="settings-field"><span className="field-label">已保存服务</span><select value={(status.providers||[]).some(p=>p.name===name)?name:''} onChange={e=>{const p=status.providers.find(x=>x.name===e.target.value);if(!p)return;setName(p.name);setBase(p.base_url||'');setTextModel(p.text_model||'');setVisionModel(p.vision_model||'');const c=(p.credentials||[]).find(x=>x.id===p.primary_credential_id)||(p.credentials||[])[0];setCredentialId(c?.id||'');setCredentialName(c?.name||'默认 Key');setKey('')}}><option value="">新建服务</option>{status.providers.map(p=><option key={p.name} value={p.name}>{p.name}</option>)}</select></label>}
      <div className="settings-grid">
        <label className="settings-field"><span className="field-label">本机服务标识</span><input aria-label="本机服务标识" value={name} onChange={e=>setName(e.target.value)} placeholder="deepseek"/><small className="field-help">本机配置别名，用于组成“服务名/模型名”，不是模型名称。</small></label>
        <label className="settings-field"><span className="field-label">OpenAI 兼容服务地址</span><input aria-label="OpenAI兼容服务地址" value={base} onChange={e=>setBase(e.target.value)} placeholder="https://api.example.com/v1"/><small className="field-help">填写服务根地址或 /v1 地址；不要填写 API Key、用户名密码或 /chat/completions。</small></label>
      </div>
      <div className="settings-grid"><label className="settings-field"><span className="field-label">Key 名称</span><input aria-label="Key名称" value={credentialName} onChange={e=>setCredentialName(e.target.value)} placeholder="工作账号"/></label><label className="settings-field"><span className="field-label">API 密钥</span><input aria-label="API密钥" type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder={credentialId?'已保存，留空以继续使用':'新增 Key 必填'}/><small className="field-help">只保存在本机受限凭据文件中，不进入日志、任务或安装包。</small></label></div>
      <div className="settings-actions"><button onClick={()=>{setCredentialId('');setCredentialName('新 Key');setKey('')}}>添加新 Key</button><label className="checkline"><input type="checkbox" checked={setDefault} onChange={e=>setSetDefault(e.target.checked)}/>设为默认服务和模型</label></div>
    </div>
    <div className="settings-card">
      <div className="settings-card-title"><strong>用途模型</strong><span>填写服务内模型名</span></div>
      <div className="settings-grid">
        <label className="settings-field"><span className="field-label">文本模型 <b>必填</b></span><input aria-label="文本模型" value={textModel} onChange={e=>setTextModel(e.target.value)} placeholder="deepseek-v4-flash"/><small className="field-help">例如 deepseek-v4-flash。无需重复填写服务名前缀。</small></label>
        <label className="settings-field"><span className="field-label">视觉模型 <b>必填</b></span><input aria-label="视觉模型" value={visionModel} onChange={e=>setVisionModel(e.target.value)} placeholder="deepseek-v4-flash-vision-exp"/><small className="field-help">用于主图观察；可以与文本模型填写相同名称。</small></label>
      </div>
      <div className="canonical-ref"><span>当前实际引用</span><div><code>文本：{textRef}</code><code>视觉：{visionRef}</code></div><small>这是系统实际发送给 Reasonix 的规范引用，用户不需要再次填写。</small></div>
    </div>
    <div className="settings-card status-card">
      <div className="settings-card-title"><strong>配置状态</strong><span>{provider?.name||name||'未配置'}</span></div>
      <div className="status-grid"><div><span>服务地址</span><b className={provider?.base_url?'ok':''}>{provider?.base_url?'已保存':'未保存'}</b></div><div><span>密钥</span><b className={provider?.authorized?'ok':''}>{provider?.authorized?'已保存':'未保存'}</b></div><div><span>文本模型</span><b className={validation?.text?.status==='validated'?'ok':''}>{validationLabel(validation?.text)}</b></div><div><span>视觉模型</span><b className={validation?.vision?.status==='validated'?'ok':''}>{validationLabel(validation?.vision)}</b></div></div>
      {validation?.validated_at&&<small className="field-help">最近验证：{new Date(validation.validated_at).toLocaleString()}</small>}
      {validation?.text?.status==='failed'&&<p className="settings-error">文本：{validation.text.message} · {validation.text.code}</p>}
      {validation?.vision?.status==='failed'&&<p className="settings-error">视觉：{validation.vision.message} · {validation.vision.code}</p>}
      {(provider?.credentials||[]).length>0&&<div className="credential-list">{provider.credentials.map(item=><div key={item.id}><span>{item.name} · {item.authorized?'已保存':'无密钥'} · {item.enabled?'启用':'停用'}{item.id===provider.primary_credential_id?' · 当前':''}</span><div><button onClick={()=>{setCredentialId(item.id);setCredentialName(item.name);setKey('')}}>编辑</button><button onClick={async()=>{const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/${item.enabled?'disable':'enable'}`,{});setStatus(value);onSaved(value)}}>{item.enabled?'停用':'启用'}</button><button disabled={item.id===provider.primary_credential_id} onClick={async()=>{const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/primary`,{});setStatus(value);onSaved(value)}}>设为当前</button><button onClick={async()=>{if(!confirm('删除这个 Key？运行中任务不会受到影响。'))return;const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/delete`,{});setStatus(value);onSaved(value)}}>删除</button></div></div>)}</div>}
    </div>
    {error&&<p className="settings-error" role="alert">{error}<br/><small>原配置仍保留；请按提示修正地址、授权或模型名称后重试。</small></p>}
    <div className="settings-actions"><button className="primary" disabled={busy||!name||!base||!textLocal||!visionLocal} onClick={submit}>{busy?'正在分别验证文本与视觉模型…':'保存并分别验证'}</button><span>{busy?'任一项失败也会保留本次配置':'文本：'+validationLabel(validation?.text)+' · 视觉：'+validationLabel(validation?.vision)}</span></div>
  </section>;
}

export function StorageDialog({api,onClose}){
 const [usage,setUsage]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const load=()=>api('/api/storage').then(setUsage);useEffect(()=>{load().catch(e=>setError(e.message))},[]);
 const labels={originals:'原始表格',original_images:'原始图片',sessions_and_credentials:'独立会话与授权',backups:'备份',database:'权威数据库',exports:'导出文件',disposable_cache:'可清理缓存'};
 const bytes=n=>n>1024**3?(n/1024**3).toFixed(2)+' GB':(n/1024**2).toFixed(2)+' MB';
 return <div className="modal-backdrop"><section className="modal"><div className="panel-heading"><strong>数据与缓存空间</strong><button onClick={onClose}>关闭</button></div><div className="modal-body">{error&&<p role="alert">{error}</p>}{usage&&<><p>数据：{usage.data_path}</p><p>缓存：{usage.cache_path}</p><p>软上限10 GB，8 GB起提醒。{usage.warning?'当前已达到提醒阈值。':''}</p><table><tbody>{Object.entries(usage.categories).map(([k,v])=><tr key={k}><td>{labels[k]}</td><td>{bytes(v)}</td></tr>)}</tbody></table><p>{usage.note}</p><button disabled={busy} onClick={async()=>{setBusy(true);setError('');try{setUsage(await api('/api/storage/clear-cache',{}))}catch(e){setError(e.message)}finally{setBusy(false)}}}>清理可重建的文字与图片缓存</button><p>目录在首次启动时选择；修改目录需要完整备份迁移，当前页面不会移动已有数据。</p></>}</div></section></div>
}
