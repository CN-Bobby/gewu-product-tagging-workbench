"""Local first-use provider configuration. Secrets are write-only in the web API.

Model references have one canonical form at the application boundary:
``provider/model``.  Older builds allowed users to paste that full reference
into the provider's model list, which produced ``provider/provider/model``
when the default reference was assembled.  The helpers below accept both
forms and persist the canonical provider-local model names.
"""
import json,os,re,subprocess,tomllib
from urllib.parse import urlparse
from dotenv import dotenv_values
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
        provider={k:p.get(k) for k in ('name','kind','base_url','models','model','default')}
        local_models=p.get('models') or ([p.get('model')] if p.get('model') else [])
        provider['models']=list(dict.fromkeys(local_model_name(p.get('name',''),m) for m in local_models if local_model_name(p.get('name',''),m)))
        provider['model_refs']=[model_reference(p.get('name',''),m) for m in provider['models']]
        provider['authorized']=bool(keys.get(p.get('api_key_env')))
        providers.append(provider)
    return {'configured':bool(providers),'providers':providers,
        'default_model':config.get('default_model'),'credential_path':str(home/'.env')}


def defaults():
    settings=read();text=settings.get('default_model') or 'deepseek/deepseek-v4-flash'
    text=normalize_reference(text)
    provider=next((p for p in settings['providers'] if p['name']==text.partition('/')[0]),None)
    models=provider.get('models') or [] if provider else []
    visual=next((m for m in models if 'vision' in m.lower()),None)
    return text,(provider['name']+'/'+visual if visual else 'deepseek/deepseek-v4-flash-vision-exp')


def save(body):
    from . import conversation,batch
    if conversation.busy or batch.active:raise ValueError('请先暂停对话和批处理，再修改模型配置')
    name=body.get('name','');base=body.get('base_url','').rstrip('/');models=body.get('models',[])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',name):raise ValueError('服务名称仅使用英文、数字、下划线或短横线')
    url=urlparse(base)
    if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment:raise ValueError('模型地址需为不含授权信息的HTTPS地址')
    if not isinstance(models,list) or not models or any(not isinstance(m,str) or not m.strip() for m in models):raise ValueError('请填写模型名称')
    home=provider_home();home.mkdir(parents=True,exist_ok=True);path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    key_name='WORKBENCH_'+name.upper().replace('-','_')+'_KEY';secret=body.get('api_key','')
    if not isinstance(secret,str) or any(c in secret for c in '\r\n\0'):raise ValueError('授权格式无效')
    keys=dotenv_values(home/'.env')
    if secret:keys[key_name]=secret
    if not keys.get(key_name):raise ValueError('首次配置需要填写授权密钥')
    models=[local_model_name(name,m) for m in models]
    models=list(dict.fromkeys(m for m in models if m))
    if not models:raise ValueError('请填写模型名称')
    provider={'name':name,'kind':'openai','base_url':base,'api_key_env':key_name,'models':models,'default':models[0]}
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
