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
  const [base,setBase]=useState('https://api.deepseek.com');
  const [key,setKey]=useState('');
  const [credentialId,setCredentialId]=useState('');
  const [modelName,setModelName]=useState('deepseek-flash');
  const [visionModelName,setVisionModelName]=useState('deepseek-flash');
  const [sameVisionModel,setSameVisionModel]=useState(true);
  const [setDefault,setSetDefault]=useState(true);
  const [legacyTextModel,setLegacyTextModel]=useState('');
  const [legacyVisionModel,setLegacyVisionModel]=useState('');
  const [savedNotice,setSavedNotice]=useState('');
  const [status,setStatus]=useState(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);

  const load=()=>api('/api/settings/models').then(value=>{
    setStatus(value);
    const provider=(value.providers||[]).find(p=>p.name===(value.default_model||'').split('/')[0])||(value.providers||[])[0];
    if(provider){
      setName(provider.name||'');
      setBase(provider.base_url||'https://api.deepseek.com');
      const models=(provider.models||[]).filter(Boolean);
      const text=localModel(provider.name,provider.text_model||provider.default||models[0]||'deepseek-flash');
      const vision=localModel(provider.name,provider.vision_model||text);
      setLegacyTextModel(text);setLegacyVisionModel(vision);
      setModelName(text||'deepseek-flash');setVisionModelName(vision||text||'deepseek-flash');setSameVisionModel(text===vision);
      const credential=(provider.credentials||[]).find(x=>x.id===provider.primary_credential_id)||(provider.credentials||[])[0];
      setCredentialId(credential?.id||'');setKey('');
    }else{
      setName('deepseek');setBase('https://api.deepseek.com');setModelName('deepseek-flash');setVisionModelName('deepseek-flash');setSameVisionModel(true);
      setCredentialId('');setKey('');setLegacyTextModel('');setLegacyVisionModel('');
    }
  });
  useEffect(()=>{load().catch(e=>setError(e.message))},[]);

  const modelLocal=localModel(name,modelName);
  const visionModelLocal=sameVisionModel?modelLocal:localModel(name,visionModelName);
  const modelRef=name&&modelLocal?`${name}/${modelLocal}`:'—';
  const visionModelRef=name&&visionModelLocal?`${name}/${visionModelLocal}`:'—';
  const provider=status?.providers?.find(p=>p.name===name);
  const credentialName=provider?.credentials?.find(item=>item.id===credentialId)?.name||'默认 Key';
  const reusableCredential=!!provider?.credentials?.some(item=>item.id===credentialId&&item.enabled&&item.authorized);
  const validation=provider?.validation;
  const imageValidation=validation?.image||validation?.vision;
  const validationMessages=[validation?.text?.status==='failed'&&`文本验证：${validation.text.message} · ${validation.text.code}`,
    imageValidation?.status==='failed'&&`图片验证：${imageValidation.message} · ${imageValidation.code}`].filter(Boolean);
  const chooseProvider=selected=>{
    setSavedNotice('');setKey('');setError('');
    if(!selected){setName('');setBase('');setModelName('deepseek-flash');setVisionModelName('deepseek-flash');setSameVisionModel(true);setCredentialId('');setLegacyTextModel('');setLegacyVisionModel('');return}
    const p=(status?.providers||[]).find(item=>item.name===selected);if(!p)return;
    setName(p.name);setBase(p.base_url||'');
    const models=(p.models||[]).filter(Boolean);
    const text=localModel(p.name,p.text_model||p.default||models[0]||'deepseek-flash');
    const vision=localModel(p.name,p.vision_model||text);
    setLegacyTextModel(text);setLegacyVisionModel(vision);setModelName(text||'deepseek-flash');setVisionModelName(vision||text||'deepseek-flash');setSameVisionModel(text===vision);
    const c=(p.credentials||[]).find(item=>item.id===p.primary_credential_id)||(p.credentials||[])[0];
    setCredentialId(c?.id||'');
  };
  const submit=async()=>{
    setBusy(true);setError('');setSavedNotice('');
    try{
      await api('/api/settings/models',{name,base_url:base,
        text_model:modelLocal,vision_model:visionModelLocal,api_key:key,credential_id:credentialId||undefined,
        credential_name:credentialName,set_default:setDefault});
      setKey('');
      const value=await api('/api/settings/models');
      setStatus(value);
      const savedProvider=value.providers?.find(item=>item.name===name);
      setCredentialId(savedProvider?.primary_credential_id||credentialId);
      const savedText=localModel(name,savedProvider?.text_model||modelLocal),savedVision=localModel(name,savedProvider?.vision_model||visionModelLocal);
      setModelName(savedText);setVisionModelName(savedVision);setSameVisionModel(savedText===savedVision);
      setLegacyTextModel(savedText);setLegacyVisionModel(savedVision);
      setSavedNotice('配置已保存；下方分别显示文本与图片验证结果。');
      onSaved(value);
    }catch(e){setError(e.message)}finally{setBusy(false)}
  };
  return <section className="provider-settings">
    <div className="settings-intro">
      <div><span className="eyebrow">本机连接</span><h3>大模型 API</h3></div>
      <p>在这里集中配置文本模型、图片识别模型和模型 Key。两个用途可以共用一个支持图片的模型。</p>
    </div>
    <div className="settings-card">
      <div className="settings-card-title"><strong>模型服务与 Key</strong><span>业务模块统一引用</span></div>
      <div className="settings-grid">
        <label className="settings-field"><span className="field-label">API 地址</span><input aria-label="API 地址" value={base} onChange={e=>{setBase(e.target.value);setSavedNotice('')}} placeholder="https://api.deepseek.com"/><small className="field-help">默认地址已填好。如服务方提供专用地址，可在这里修改。</small></label>
        <label className="settings-field"><span className="field-label">文本模型</span><input aria-label="文本模型" value={modelName} onChange={e=>{setModelName(e.target.value);setSavedNotice('')}} placeholder="deepseek-v4-flash"/><small className="field-help">填写服务方提供的模型名。</small></label>
      </div>
      <label className="checkline"><input type="checkbox" checked={sameVisionModel} onChange={e=>{setSameVisionModel(e.target.checked);setSavedNotice('')}}/>文本模型支持图片识别，图片任务也使用此模型</label>
      {!sameVisionModel&&<label className="settings-field"><span className="field-label">图片识别模型</span><input aria-label="图片识别模型" value={visionModelName} onChange={e=>{setVisionModelName(e.target.value);setSavedNotice('')}} placeholder="qwen3.8-flash"/><small className="field-help">可与文本模型不同，但共用当前服务和 Key。</small></label>}
      <label className="settings-field"><span className="field-label">API Key</span><input aria-label="API Key" type="password" autoComplete="new-password" value={key} onChange={e=>{setKey(e.target.value);setSavedNotice('')}} placeholder={credentialId?'已保存；留空继续使用当前 Key':'请输入 API Key'}/><small className="field-help">编辑时留空会继续使用已保存的 Key；这里不会回显密钥。</small></label>
      <div className="canonical-ref"><span>默认模型引用</span><div><code>文本：{modelRef}</code><code>图片：{visionModelRef}</code></div><small>一个模型 Key 可同时通过文本和图片能力验证。</small></div>
      <div className="settings-actions"><button className="primary" disabled={busy||!name.trim()||!base.trim()||!modelLocal.trim()||!visionModelLocal.trim()||(!reusableCredential&&!key.trim())} onClick={submit}>{busy?'正在保存并验证…':'保存并验证'}</button><span>{reusableCredential&&!key?'已选择本机保存的 Key':''}</span></div>
      {savedNotice&&<p className="settings-saved" role="status">{savedNotice}</p>}
      {error&&<p className="settings-error" role="alert">保存失败：{error}<br/><small>如果配置已成功保存，验证失败原因会显示在文本或图片状态中。</small></p>}
    </div>
    <div className="settings-card status-card">
      <div className="settings-card-title"><strong>配置状态</strong><span>{provider?.name||name||'未配置'}</span></div>
      <div className="status-grid"><div><span>模型与地址</span><b className={provider?.base_url?'ok':''}>{provider?.base_url?'已保存':'未保存'}</b></div><div><span>API Key</span><b className={provider?.authorized?'ok':''}>{provider?.authorized?'已保存':'未保存'}</b></div><div><span>文本请求</span><b className={validation?.text?.status==='validated'?'ok':''}>{validationLabel(validation?.text)}</b></div><div><span>图片请求</span><b className={imageValidation?.status==='validated'?'ok':''}>{validationLabel(imageValidation)}</b></div></div>
      {validation?.validated_at&&<small className="field-help">最近验证：{new Date(validation.validated_at).toLocaleString()}</small>}
      {validationMessages.map(message=><p key={message} className="settings-error">{message}</p>)}
    </div>
    <details className="settings-card settings-advanced">
      <summary>高级服务与 Key 管理</summary>
      <p className="field-help">在这里切换已保存服务并管理多个 Key。文本和图片识别模型分别保存，任务创建时会冻结当前模型及 Key 编号。</p>
      {(status?.providers||[]).length>0&&<label className="settings-field"><span className="field-label">已保存服务</span><select aria-label="已保存服务" value={(status.providers||[]).some(p=>p.name===name)?name:''} onChange={event=>chooseProvider(event.target.value)}><option value="">新建兼容服务…</option>{status.providers.map(p=><option key={p.name} value={p.name}>{p.name}</option>)}</select></label>}
      <div className="settings-grid"><label className="settings-field"><span className="field-label">服务标识</span><input aria-label="服务标识" value={name} onChange={event=>{setName(event.target.value);setCredentialId('');setSavedNotice('')}} placeholder="deepseek"/></label><label className="settings-field"><span className="field-label">默认服务</span><span className="checkline"><input type="checkbox" checked={setDefault} onChange={event=>setSetDefault(event.target.checked)}/>保存后设为全局默认</span></label></div>
      <div className="canonical-ref"><span>已保存模型引用</span><div><code>文本：{legacyTextModel?`${name}/${legacyTextModel}`:'—'}</code><code>图片：{legacyVisionModel?`${name}/${legacyVisionModel}`:'—'}</code></div></div>
      <div className="settings-actions"><button onClick={()=>{setCredentialId('');setKey('');setSavedNotice('请输入新 Key，再点上方“保存并验证”。')}}>添加新 Key</button></div>
      {(provider?.credentials||[]).length>0&&<div className="credential-list"><strong>本机 Key</strong>{provider.credentials.map(item=><div key={item.id}><span>{item.name} · {item.authorized?'已保存':'无密钥'} · {item.enabled?'启用':'停用'}{item.id===provider.primary_credential_id?' · 当前':''}</span><div><button onClick={()=>{setCredentialId(item.id);setKey('');setSavedNotice('已选中此 Key；API Key 留空会复用它。')}}>选用</button><button onClick={async()=>{const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/${item.enabled?'disable':'enable'}`,{});setStatus(value);onSaved(value)}}>{item.enabled?'停用':'启用'}</button><button disabled={item.id===provider.primary_credential_id} onClick={async()=>{const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/primary`,{});setStatus(value);onSaved(value)}}>设为当前</button><button onClick={async()=>{if(!confirm('删除这个 Key？运行中任务不会受到影响。'))return;const value=await api(`/api/settings/models/${provider.name}/credentials/${item.id}/delete`,{});setStatus(value);onSaved(value);if(item.id===credentialId)setCredentialId(value.providers?.find(p=>p.name===name)?.primary_credential_id||'')}}>删除</button></div></div>)}</div>}
    </details>
  </section>;
}

export function StorageDialog({api,onClose}){
 const [usage,setUsage]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const load=()=>api('/api/storage').then(setUsage);useEffect(()=>{load().catch(e=>setError(e.message))},[]);
 const labels={originals:'原始表格',original_images:'原始图片',sessions_and_credentials:'独立会话与授权',backups:'备份',database:'权威数据库',exports:'导出文件',keyword_research:'关键词调研',disposable_cache:'可清理缓存'};
 const bytes=n=>n>1024**3?(n/1024**3).toFixed(2)+' GB':(n/1024**2).toFixed(2)+' MB';
 return <div className="modal-backdrop"><section className="modal"><div className="panel-heading"><strong>数据与缓存空间</strong><button onClick={onClose}>关闭</button></div><div className="modal-body">{error&&<p role="alert">{error}</p>}{usage&&<><p>数据：{usage.data_path}</p><p>缓存：{usage.cache_path}</p><p>软上限10 GB，8 GB起提醒。{usage.warning?'当前已达到提醒阈值。':''}</p><table><tbody>{Object.entries(usage.categories).map(([k,v])=><tr key={k}><td>{labels[k]}</td><td>{bytes(v)}</td></tr>)}</tbody></table><p>{usage.note}</p><button disabled={busy} onClick={async()=>{setBusy(true);setError('');try{setUsage(await api('/api/storage/clear-cache',{}))}catch(e){setError(e.message)}finally{setBusy(false)}}}>清理可重建的文字与图片缓存</button><p>目录在首次启动时选择；修改目录需要完整备份迁移，当前页面不会移动已有数据。</p></>}</div></section></div>
}
