"""Local first-use provider configuration. Secrets are write-only in the web API.

Model references have one canonical form at the application boundary:
``provider/model``.  Older builds allowed users to paste that full reference
into the provider's model list, which produced ``provider/provider/model``
when the default reference was assembled.  The helpers below accept both
forms and persist the canonical provider-local model names.
"""
import base64
import json,os,re,subprocess,tomllib
from urllib.parse import urlparse
from dotenv import dotenv_values
import httpx
from .runtime_isolation import provider_home,_toml


def local_model_name(provider_name, value):
    """Return a provider-local model id from a pasted or local value."""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    prefix = provider_name.strip() + '/'
    # Remove repeated provider prefixes introduced by pre-fix UI versions.
    while value.startswith(prefix):
        value = value[len(prefix):]
    return value.strip('/')


def model_reference(provider_name, value):
    """Return the stable ``provider/model`` reference used by ACP and jobs."""
    local = local_model_name(provider_name, value)
    return provider_name.strip() + '/' + local if local else provider_name.strip()


def normalize_reference(value):
    """Normalize a possibly duplicated provider/model reference.

    References without a slash are left alone because the caller may be
    passing a provider default that still needs provider lookup.
    """
    if not isinstance(value, str):
        return value
    name, sep, model = value.strip().partition('/')
    return model_reference(name, model) if sep else value.strip()


def _canonical_config(config):
    """Canonicalize provider model fields without dropping unknown settings."""
    changed = False
    providers = config.get('providers', [])
    if not isinstance(providers, list):
        return config, changed
    for provider in providers:
        if not isinstance(provider, dict) or not isinstance(provider.get('name'), str):
            continue
        name = provider['name'].strip()
        raw_models = provider.get('models')
        if raw_models is None:
            raw_models = [provider.get('model')] if provider.get('model') else []
        if not isinstance(raw_models, list):
            raw_models = [raw_models]
        models = list(dict.fromkeys(local_model_name(name, m) for m in raw_models if local_model_name(name, m)))
        if provider.get('models') != models:
            provider['models'] = models
            changed = True
        if provider.get('model') is not None:
            local = local_model_name(name, provider.get('model'))
            if provider.get('model') != local:
                provider['model'] = local
                changed = True
        default = provider.get('default')
        if default is not None:
            local = local_model_name(name, default)
            if default != local:
                provider['default'] = local
                changed = True
        # Newer clients store the two capabilities explicitly.  Derive them
        # from the legacy list once so old Reasonix configurations continue to
        # work without making the user re-enter model names.
        text = provider.get('text_model') or provider.get('default') or (models[0] if models else '')
        text = local_model_name(name, text)
        visual = provider.get('vision_model')
        if not visual:
            visual = next((m for m in models if 'vision' in m.lower()), None)
        if not visual and len(models) > 1:
            visual = models[1]
        if not visual:
            visual = text
        visual = local_model_name(name, visual)
        for key, value in (('text_model', text), ('vision_model', visual)):
            if value and provider.get(key) != value:
                provider[key] = value
                changed = True
        if text and provider.get('default') != text:
            provider['default'] = text
            changed = True
    default_ref = config.get('default_model')
    if isinstance(default_ref, str) and '/' in default_ref:
        name, _, model = default_ref.partition('/')
        canonical = model_reference(name, model)
        if default_ref != canonical:
            config['default_model'] = canonical
            changed = True
    return config, changed


def _write_config(path, config):
    path.write_text('\n'.join(json.dumps(k)+' = '+_toml(v) for k,v in config.items()), encoding='utf-8')


def read():
    home=provider_home();path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    config, changed = _canonical_config(config)
    # Migrate the malformed but otherwise valid configuration on first read.
    # This is intentionally limited to provider model fields and keeps secrets
    # in the existing write-only .env file.
    if changed and path.exists():
        _write_config(path, config)
    keys=dotenv_values(home/'.env')
    providers=[]
    for p in config.get('providers',[]):
        provider={k:p.get(k) for k in ('name','kind','base_url','models','model','default','text_model','vision_model','validation')}
        local_models=p.get('models') or ([p.get('model')] if p.get('model') else [])
        provider['models']=list(dict.fromkeys(local_model_name(p.get('name',''),m) for m in local_models if local_model_name(p.get('name',''),m)))
        provider['text_model']=local_model_name(p.get('name',''),p.get('text_model') or p.get('default') or (provider['models'][0] if provider['models'] else ''))
        provider['vision_model']=local_model_name(p.get('name',''),p.get('vision_model') or next((m for m in provider['models'] if 'vision' in m.lower()),None) or (provider['models'][1] if len(provider['models'])>1 else provider['text_model']))
        provider['model_refs']=[model_reference(p.get('name',''),m) for m in provider['models']]
        provider['authorized']=bool(keys.get(p.get('api_key_env')))
        providers.append(provider)
    return {'configured':bool(providers),'providers':providers,
        'default_model':config.get('default_model'),'credential_path':str(home/'.env')}


def defaults():
    settings=read();text=settings.get('default_model') or 'deepseek/deepseek-v4-flash'
    text=normalize_reference(text)
    provider=next((p for p in settings['providers'] if p['name']==text.partition('/')[0]),None)
    if provider:
        text=model_reference(provider['name'],provider.get('text_model') or text.partition('/')[2])
        visual=provider.get('vision_model') or provider.get('text_model')
        return text,model_reference(provider['name'],visual)
    return text,'deepseek/deepseek-v4-flash-vision-exp'


_TEST_IMAGE='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='


def _safe_response_error(response, capability):
    """Return a short diagnostic without echoing credentials or payloads."""
    detail=''
    try:
        value=response.json()
        if isinstance(value,dict):
            error=value.get('error')
            detail=error.get('message','') if isinstance(error,dict) else value.get('message','')
    except Exception:
        detail=''
    detail=re.sub(r'(?i)bearer\s+[^\s]+','Bearer ***',str(detail))
    detail=detail.replace('\n',' ').strip()[:240]
    suffix='：'+detail if detail else ''
    return f'{capability}模型验证失败（HTTP {response.status_code}）{suffix}'


def _validate_request(endpoint, api_key, model, capability, content):
    try:
        with httpx.Client(timeout=20) as client:
            response=client.post(endpoint,headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},json={
                'model':model,'messages':[{'role':'user','content':content}], 'max_tokens':1})
    except httpx.TimeoutException as exc:
        raise ValueError(f'{capability}模型验证超时，请检查服务地址或网络') from exc
    except httpx.HTTPError as exc:
        raise ValueError(f'{capability}模型验证网络失败，请检查服务地址：{exc.__class__.__name__}') from exc
    if response.status_code in (401,403):
        raise ValueError(f'{capability}模型验证失败（授权无效，HTTP {response.status_code}）')
    if response.status_code==404:
        raise ValueError(f'{capability}模型验证失败（模型或接口不存在，HTTP 404）')
    if response.status_code!=200:
        raise ValueError(_safe_response_error(response,capability))
    try:
        data=response.json()
    except Exception as exc:
        raise ValueError(f'{capability}模型验证失败：服务返回不是合法 JSON') from exc
    if not isinstance(data,dict) or not data.get('choices'):
        raise ValueError(f'{capability}模型验证失败：服务未返回可用结果')


def validate_models(base_url, api_key, text_model, vision_model):
    """Probe both capabilities with non-business, one-token requests."""
    endpoint=base_url.rstrip('/')+'/chat/completions'
    _validate_request(endpoint,api_key,text_model,'文本','连接测试，仅回复 OK')
    vision_content=[{'type':'text','text':'连接测试，仅回复 OK'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+_TEST_IMAGE}}]
    _validate_request(endpoint,api_key,vision_model,'视觉',vision_content)
    return {'status':'validated','validated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            'text_model':text_model,'vision_model':vision_model}


def _local_input(name, value, label):
    if not isinstance(value,str):
        raise ValueError(f'请填写{label}')
    value=local_model_name(name,value)
    if not value or '/' in value or any(ord(c)<32 for c in value):
        raise ValueError(f'{label}请填写服务内模型名，例如 deepseek-v4-flash')
    return value


def save(body):
    from . import conversation,batch
    if conversation.busy or batch.active:raise ValueError('请先暂停对话和批处理，再修改模型配置')
    name=body.get('name','');base=body.get('base_url','').rstrip('/')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',name):raise ValueError('服务名称仅使用英文、数字、下划线或短横线')
    url=urlparse(base)
    if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment or url.path.lower().endswith('/chat/completions'):raise ValueError('模型地址需为不含授权信息的HTTPS服务根地址或 /v1 地址，不能填写 /chat/completions')
    legacy=body.get('models',[])
    if legacy is None:legacy=[]
    if not isinstance(legacy,list) or any(not isinstance(m,str) or not m.strip() for m in legacy):raise ValueError('旧格式模型列表无效')
    text_raw=body.get('text_model') or (legacy[0] if legacy else '')
    visual_raw=body.get('vision_model') or next((m for m in legacy if 'vision' in m.lower()),None) or (legacy[1] if len(legacy)>1 else text_raw)
    text_model=_local_input(name,text_raw,'文本模型')
    vision_model=_local_input(name,visual_raw,'视觉模型')
    home=provider_home();home.mkdir(parents=True,exist_ok=True);path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    key_name='WORKBENCH_'+name.upper().replace('-','_')+'_KEY';secret=body.get('api_key','')
    if not isinstance(secret,str) or any(c in secret for c in '\r\n\0'):raise ValueError('授权格式无效')
    keys=dotenv_values(home/'.env')
    if secret:keys[key_name]=secret
    if not keys.get(key_name):raise ValueError('首次配置需要填写授权密钥')
    validation=validate_models(base,keys[key_name],text_model,vision_model)
    models=list(dict.fromkeys([text_model,vision_model]+[local_model_name(name,m) for m in legacy if local_model_name(name,m)]))
    provider={'name':name,'kind':'openai','base_url':base,'api_key_env':key_name,'models':models,'default':text_model,
              'text_model':text_model,'vision_model':vision_model,'validation':validation}
    config['providers']=[p for p in config.get('providers',[]) if p.get('name')!=name]+[provider]
    config['default_model']=model_reference(name,models[0])
    # Preserve existing non-provider settings, including native client permissions and plugins.
    _write_config(path,config)
    secrets=home/'.env';secrets.write_text('\n'.join(k+'='+json.dumps(v) for k,v in keys.items() if isinstance(v,str)),encoding='utf-8')
    if os.name=='nt':
        user=subprocess.check_output(['whoami'],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
        result=subprocess.run(['icacls',str(secrets),'/inheritance:r','/grant:r',user+':F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise ValueError('授权文件访问权限设置失败')
    conversation.close()
    return read()
