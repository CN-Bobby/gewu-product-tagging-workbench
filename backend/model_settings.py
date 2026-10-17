"""Local first-use provider configuration. Secrets are write-only in the web API.

Model references have one canonical form at the application boundary:
``provider/model``.  Older builds allowed users to paste that full reference
into the provider's model list, which produced ``provider/provider/model``
when the default reference was assembled.  The helpers below accept both
forms and persist the canonical provider-local model names.
"""
import base64
import io
import json,os,re,subprocess,tomllib,uuid
import time
from urllib.parse import urlparse
import httpx
from .runtime_isolation import provider_home,_toml
from .credential_vault import get_secret, set_secret, delete_secret


class SettingsInUse(ValueError):
    """A settings mutation was refused because a runtime could still be relying on the configuration.

    V1 is a single conservative lock rather than a per-field policy: while any workload of this
    process is live, nothing that a launch resolves may change. Removing or repointing a key under a
    running runtime is what silently moves it onto a different credential, and that is the failure
    this refuses to allow. ``ValueError`` stays as the base so callers that only know a refused
    configuration keep working.
    """
    code='settings_in_use'


def assert_settings_mutable():
    """Refuse a settings mutation while this process owns a live runtime or a batch job.

    Read-only: it asks each owner whether it has active work, and builds or changes nothing. The
    imports are local because each of those modules reaches back into this one while starting up.
    """
    from . import batch
    from .agent_application import has_active_sessions
    from .jobs import has_active_reasonix_work
    active=[]
    if batch.active:active.append('批处理')
    if has_active_reasonix_work():active.append('后台打标任务')
    if has_active_sessions():active.append('Agent 会话')
    if active:
        raise SettingsInUse('有正在运行的'+ '、'.join(active) +'，请先停止后再修改模型配置')


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
        credentials=provider.get('credentials')
        if not isinstance(credentials,list):credentials=[]
        if provider.get('api_key_env') and not any(x.get('api_key_env')==provider.get('api_key_env') for x in credentials if isinstance(x,dict)):
            credentials.insert(0,{'id':'legacy','name':'Default Key','api_key_env':provider.get('api_key_env'),'enabled':True,'validation':provider.get('validation')})
            changed=True
        credentials=[x for x in credentials if isinstance(x,dict) and x.get('id') and x.get('api_key_env')]
        if credentials:
            primary=provider.get('primary_credential_id')
            if not any(x['id']==primary for x in credentials):primary=credentials[0]['id']
            provider['credentials']=credentials;provider['primary_credential_id']=primary
            chosen=next(x for x in credentials if x['id']==primary)
            if provider.get('api_key_env')!=chosen['api_key_env']:provider['api_key_env']=chosen['api_key_env'];changed=True
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
    if changed and path.exists():
        _write_config(path, config)
    providers=[]
    for p in config.get('providers',[]):
        provider={k:p.get(k) for k in ('name','kind','base_url','models','model','default','text_model','vision_model','validation','primary_credential_id')}
        local_models=p.get('models') or ([p.get('model')] if p.get('model') else [])
        provider['models']=list(dict.fromkeys(local_model_name(p.get('name',''),m) for m in local_models if local_model_name(p.get('name',''),m)))
        provider['text_model']=local_model_name(p.get('name',''),p.get('text_model') or p.get('default') or (provider['models'][0] if provider['models'] else ''))
        provider['vision_model']=local_model_name(p.get('name',''),p.get('vision_model') or next((m for m in provider['models'] if 'vision' in m.lower()),None) or (provider['models'][1] if len(provider['models'])>1 else provider['text_model']))
        provider['model_refs']=[model_reference(p.get('name',''),m) for m in provider['models']]
        provider['credentials']=[{'id':x.get('id'),'name':x.get('name') or 'Unnamed Key','enabled':x.get('enabled',True),'authorized':bool(get_secret('model',x.get('api_key_env'))) if x.get('api_key_env') else False,'validation':x.get('validation')} for x in p.get('credentials',[]) if isinstance(x,dict)]
        provider['authorized']=any(x['authorized'] and x['enabled'] for x in provider['credentials']) or (bool(get_secret('model',p.get('api_key_env'))) if p.get('api_key_env') else False)
        providers.append(provider)
    return {'configured':bool(providers),'providers':providers,
        'default_model':config.get('default_model'),'credential_path':'Windows 当前用户 DPAPI 凭证库'}


def defaults():
    settings=read();text=settings.get('default_model') or 'deepseek/deepseek-flash'
    text=normalize_reference(text)
    provider=next((p for p in settings['providers'] if p['name']==text.partition('/')[0]),None)
    if provider:
        text=model_reference(provider['name'],provider.get('text_model') or text.partition('/')[2])
        visual=provider.get('vision_model') or provider.get('text_model')
        return text,model_reference(provider['name'],visual)
    return text,'deepseek/deepseek-flash'


def _vision_fixture():
    """Build a common-provider-safe semantic probe containing no business data."""
    from PIL import Image,ImageDraw
    image=Image.new('RGB',(512,512),'white');draw=ImageDraw.Draw(image)
    draw.ellipse((35,176,155,296),fill=(220,35,45),outline='black',width=5)
    draw.polygon([(256,166),(190,296),(322,296)],fill=(30,165,75),outline='black')
    draw.rectangle((357,176,477,296),fill=(35,90,220),outline='black',width=5)
    buffer=io.BytesIO();image.save(buffer,'JPEG',quality=92);payload=buffer.getvalue()
    with Image.open(io.BytesIO(payload)) as check:
        if check.format!='JPEG' or check.size!=(512,512) or check.mode!='RGB':raise RuntimeError('test_fixture_invalid')
        check.verify()
    return base64.b64encode(payload).decode()

class ProbeFailure(RuntimeError):
    def __init__(self,code,message,http_status=None):
        super().__init__(message);self.code=code;self.http_status=http_status


def _safe_response_error(response, capability, secret=''):
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
    detail=re.sub(r'https?://[^\s\"\'<>]+','[服务地址已隐藏]',detail)
    detail=re.sub(r'\bB0[A-Z0-9]{8}\b','[商品编号已隐藏]',detail,flags=re.I)
    if secret:
        detail=detail.replace(secret,'***')
    detail=detail.replace('\n',' ').strip()[:240]
    suffix='：'+detail if detail else ''
    return f'{capability}模型验证失败（HTTP {response.status_code}）{suffix}'


def _response_detail(response,secret=''):
    text=_safe_response_error(response,'',secret)
    return text.replace('模型验证失败','').strip('： ')

def _validate_request(endpoint, api_key, model, capability, content, expected_order=None, usage_context=None):
    usage_context=usage_context or {}
    try:
        host=urlparse(endpoint).hostname or ''
        body={'model':model,'messages':[{'role':'user','content':content}],
              'max_tokens':512 if expected_order else 32}
        if host.casefold() in ('api.deepseek.com','api.deepseek.cn'):
            body['thinking']={'type':'disabled'}
        usage_event_id=None
        try:
            from .usage import start_usage_event,provider_name
            usage_event_id=start_usage_event(kind='model',module='model_settings',
                task_id=usage_context.get('task_id'),task_title=usage_context.get('task_title'),
                model=model,provider=provider_name(model,endpoint),attempt=1,
                request_text=body,metadata={'capability':capability})
        except Exception:
            pass
        started=time.perf_counter()
        try:
            with httpx.Client(timeout=20) as client:
                response=client.post(endpoint,headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},json=body)
        except Exception:
            try:
                from .usage import finish_usage_event
                finish_usage_event(usage_event_id,status='network_error',
                    duration_ms=round((time.perf_counter()-started)*1000),request_text=body)
            except Exception:
                pass
            raise
    except httpx.TimeoutException as exc:
        raise ProbeFailure('network_error',f'{capability}模型验证超时，请检查服务地址或网络') from exc
    except httpx.HTTPError as exc:
        raise ProbeFailure('network_error',f'{capability}模型验证网络失败，请检查服务地址：{exc.__class__.__name__}') from exc
    try:
        response_data=response.json()
    except Exception:
        response_data=None
    response_text=''
    if isinstance(response_data,dict):
        choices=response_data.get('choices') or []
        message=(choices[0].get('message') or {}) if choices else {}
        response_text=message.get('content') if isinstance(message,dict) else ''
    try:
        from .usage import finish_usage_event
        finish_usage_event(usage_event_id,
            status='success' if response.status_code==200 and isinstance(response_data,dict) else
                   'failed' if response.status_code!=200 else 'invalid',
            duration_ms=round((time.perf_counter()-started)*1000),
            usage=(response_data.get('usage') or {}) if isinstance(response_data,dict) else None,
            request_text=body,response_text=response_text or response.text)
    except Exception:
        pass
    detail=_response_detail(response,api_key);lower=detail.casefold()
    if response.status_code in (401,403):
        raise ProbeFailure('authentication_failed',f'{capability}模型鉴权失败（HTTP {response.status_code}）',response.status_code)
    if response.status_code==404:
        code='model_not_found' if 'model' in lower or '模型' in lower else 'endpoint_not_found'
        raise ProbeFailure(code,f'{capability}'+('模型名称不存在' if code=='model_not_found' else '接口地址不存在')+'（HTTP 404）',404)
    if response.status_code==429:raise ProbeFailure('rate_limited',f'{capability}模型验证受到限流（HTTP 429）',429)
    if capability=='视觉' and response.status_code in (400,415,422) and any(x in lower for x in ('image','vision','multimodal','图片','视觉')):
        code='test_image_invalid' if any(x in lower for x in ('invalid image','image size','format','decode','尺寸','格式')) else 'vision_unsupported'
        raise ProbeFailure(code,'视觉模型验证失败：'+('服务拒绝测试图片' if code=='test_image_invalid' else '模型不支持视觉输入'),response.status_code)
    if response.status_code!=200:
        raise ProbeFailure('provider_error',_safe_response_error(response,capability,api_key),response.status_code)
    try:
        data=response.json()
    except Exception as exc:
        raise ProbeFailure('response_contract_failed',f'{capability}模型验证失败：服务返回不是合法 JSON',response.status_code) from exc
    if not isinstance(data,dict) or not data.get('choices'):
        raise ProbeFailure('response_contract_failed',f'{capability}模型验证失败：服务未返回可用结果，请检查模型名、服务兼容格式和输出预算',response.status_code)
    if expected_order:
        choice=data['choices'][0]
        raw=(choice.get('message') or {}).get('content')
        try:value=json.loads(raw) if isinstance(raw,str) else raw
        except Exception:value=None
        if not isinstance(value,dict) or value.get('order')!=expected_order:
            if choice.get('finish_reason')=='length':
                message='视觉请求被输出长度限制截断；请检查模型的最大输出限制'
            elif not raw:
                message='视觉请求成功但没有正文；请确认模型支持图片输入，并启用非思考模式'
            else:
                message='视觉请求成功但未按要求返回图形顺序；请确认模型支持图片输入与 JSON 输出'
            raise ProbeFailure('response_contract_failed',message,response.status_code)
    return data

def _probe(endpoint,api_key,model,capability,content,expected_order=None,task_id=None):
    checked=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    try:
        _validate_request(endpoint,api_key,model,capability,content,expected_order,
            usage_context={'task_id':task_id,'task_title':'模型设置连接测试 · '+capability})
        return {'status':'validated','code':None,'message':'验证通过','model':model,'checked_at':checked,'http_status':200}
    except ProbeFailure as exc:
        return {'status':'failed','code':exc.code,'message':str(exc),'model':model,'checked_at':checked,'http_status':exc.http_status}
    except Exception as exc:
        return {'status':'failed','code':'validator_internal_error','message':'验证程序内部错误：'+type(exc).__name__,'model':model,'checked_at':checked,'http_status':None}


def validate_models(base_url, api_key, text_model, vision_model):
    """Probe text and vision independently, retaining both outcomes."""
    from .external_effects import assert_allowed
    assert_allowed('model_validation')
    endpoint=base_url.rstrip('/')+'/chat/completions'
    probe_id='probe_'+uuid.uuid4().hex
    text=_probe(endpoint,api_key,text_model,'文本','连接测试，仅回复 OK',task_id=probe_id)
    try:image=_vision_fixture()
    except Exception as exc:
        checked=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        vision={'status':'failed','code':'validator_internal_error','message':'标准测试图片生成或校验失败：'+str(exc),'model':vision_model,'checked_at':checked,'http_status':None}
    else:
        order=['circle','triangle','square']
        vision_content=[{'type':'text','text':'观察图片，从左到右识别三个图形。只返回JSON：{"order":["shape1","shape2","shape3"]}，图形名仅用 circle、triangle、square。'},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+image}}]
        vision=_probe(endpoint,api_key,vision_model,'视觉',vision_content,order,probe_id)
    statuses=[text['status'],vision['status']];overall='validated' if statuses==['validated','validated'] else 'partial' if 'validated' in statuses else 'failed'
    return {'status':overall,'overall':overall,'validated_at':max(text['checked_at'],vision['checked_at']),
            'text_model':text_model,'vision_model':vision_model,'text':text,'vision':vision}

def require_validated(model_ref,credential_id,capability):
    """Reject explicitly failed modern probes without breaking legacy configs."""
    settings=read();provider_name=normalize_reference(model_ref).partition('/')[0]
    provider=next((p for p in settings['providers'] if p['name']==provider_name),None)
    if not provider:raise ValueError('模型服务不存在：'+provider_name)
    credential=next((x for x in provider.get('credentials',[]) if x.get('id')==credential_id),None) if credential_id else next((x for x in provider.get('credentials',[]) if x.get('id')==provider.get('primary_credential_id')),None)
    validation=(credential or {}).get('validation') or provider.get('validation') or {};result=validation.get(capability) if isinstance(validation,dict) else None
    if isinstance(result,dict) and result.get('status')=='failed':raise ValueError(result.get('message') or ('模型'+capability+'能力未通过验证'))
    return True


def _local_input(name, value, label):
    if not isinstance(value,str):
        raise ValueError(f'请填写{label}')
    value=local_model_name(name,value)
    if not value or any(ord(c)<32 for c in value):
        raise ValueError(f'{label}请填写合法的服务内模型名，例如 org/model-name')
    return value


def save(body):
    assert_settings_mutable()
    name=body.get('name','');base=body.get('base_url','').rstrip('/')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',name):raise ValueError('服务名称仅使用英文、数字、下划线或短横线')
    url=urlparse(base)
    if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment or url.path.lower().endswith('/chat/completions'):raise ValueError('模型地址需为不含授权信息的HTTPS服务根地址或 /v1 地址，不能填写 /chat/completions')
    legacy=body.get('models',[])
    if legacy is None:legacy=[]
    if not isinstance(legacy,list) or any(not isinstance(m,str) or not m.strip() for m in legacy):raise ValueError('旧格式模型列表无效')
    unified=body.get('model')
    text_raw=unified or body.get('text_model') or (legacy[0] if legacy else '')
    visual_raw=(unified or body.get('vision_model') or next((m for m in legacy if 'vision' in m.lower()),None) or (legacy[1] if len(legacy)>1 else text_raw))
    text_model=_local_input(name,text_raw,'文本模型')
    vision_model=_local_input(name,visual_raw,'视觉模型')
    home=provider_home();home.mkdir(parents=True,exist_ok=True);path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    config,_=_canonical_config(config)
    existing=next((p for p in config.get('providers',[]) if p.get('name')==name),None) or {}
    existing_credentials=list(existing.get('credentials') or [])
    credential_id=str(body.get('credential_id') or existing.get('primary_credential_id') or uuid.uuid4().hex[:12])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',credential_id):raise ValueError('Key 标识无效')
    current=next((x for x in existing_credentials if x.get('id')==credential_id),None)
    key_name=(current or {}).get('api_key_env') or ('WORKBENCH_'+name.upper().replace('-','_')+'_'+credential_id.upper().replace('-','_')+'_KEY')
    secret=body.get('api_key','')
    if not isinstance(secret,str) or any(c in secret for c in '\r\n\0'):raise ValueError('授权格式无效')
    current_secret=get_secret('model',key_name)
    selected_secret=secret or current_secret
    if not selected_secret:raise ValueError('首次配置需要填写授权密钥')
    try:
        from .external_effects import effects_allowed
        can_validate=effects_allowed('model_validation')
    except ImportError:
        # A partial/legacy package must not turn a missing policy module into an
        # implicit permission to contact a paid model service.
        can_validate=False
    validation=validate_models(base,selected_secret,text_model,vision_model) if can_validate else {'status':'not_run','overall':'not_run','message':'当前运行实例明确设置为离线；未发送模型请求'}
    credential={'id':credential_id,'name':str(body.get('credential_name') or (current or {}).get('name') or 'Default Key').strip()[:100] or 'Default Key','api_key_env':key_name,'enabled':body.get('credential_enabled',True),'validation':validation}
    credentials=[x for x in existing_credentials if x.get('id')!=credential_id]+[credential]
    models=list(dict.fromkeys([text_model,vision_model]+list(existing.get('models') or [])+[local_model_name(name,m) for m in legacy if local_model_name(name,m)]))
    provider={**existing,'name':name,'kind':'openai','base_url':base,'api_key_env':key_name,'credentials':credentials,'primary_credential_id':credential_id,'models':models,'default':text_model,
              'text_model':text_model,'vision_model':vision_model,'validation':validation}
    config['providers']=[p for p in config.get('providers',[]) if p.get('name')!=name]+[provider]
    if body.get('set_default',True):config['default_model']=model_reference(name,text_model)
    # Preserve existing non-provider settings, including native client permissions and plugins.
    previous_secret=current_secret
    if secret:set_secret('model',key_name,secret)
    try:
        _write_config(path,config)
    except BaseException:
        if secret:
            if previous_secret:set_secret('model',key_name,previous_secret)
            else:delete_secret('model',key_name)
        raise
    return read()


def credential_action(provider_name,credential_id,action):
    assert_settings_mutable()
    home=provider_home();path=home/'config.toml'
    if not path.exists():raise ValueError('模型配置不存在')
    config=tomllib.loads(path.read_text(encoding='utf-8-sig'));config,_=_canonical_config(config)
    provider=next((p for p in config.get('providers',[]) if p.get('name')==provider_name),None)
    if not provider:raise ValueError('服务不存在')
    credentials=list(provider.get('credentials') or []);target=next((x for x in credentials if x.get('id')==credential_id),None)
    if not target:raise ValueError('Key 不存在')
    if action=='delete':
        credentials=[x for x in credentials if x.get('id')!=credential_id]
        key_to_delete=target.get('api_key_env')
        provider['credentials']=credentials
        if credentials:
            provider['primary_credential_id']=credentials[0]['id'];provider['api_key_env']=credentials[0]['api_key_env']
        else:provider['primary_credential_id']=None;provider['api_key_env']=None
    elif action in ('enable','disable'):
        target['enabled']=action=='enable';provider['credentials']=credentials
    elif action=='primary':
        if not target.get('enabled',True):raise ValueError('停用的 Key 不能设为当前 Key')
        provider['primary_credential_id']=credential_id;provider['api_key_env']=target['api_key_env']
    else:raise ValueError('未知 Key 操作')
    _write_config(path,config)
    if action=='delete' and key_to_delete:delete_secret('model',key_to_delete)
    return read()
