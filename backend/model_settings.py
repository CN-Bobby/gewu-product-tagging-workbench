"""Local first-use provider configuration. Secrets are write-only in the web API."""
import json,os,re,subprocess,tomllib
from urllib.parse import urlparse
from dotenv import dotenv_values
from .runtime_isolation import provider_home,_toml


def read():
    home=provider_home();path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    keys=dotenv_values(home/'.env')
    return {'configured':bool(config.get('providers')),'providers':[
        {k:p.get(k) for k in ('name','kind','base_url','models','model','default')}|
        {'authorized':bool(keys.get(p.get('api_key_env')))} for p in config.get('providers',[])],
        'default_model':config.get('default_model'),'credential_path':str(home/'.env')}


def defaults():
    settings=read();text=settings.get('default_model') or 'deepseek/deepseek-v4-flash'
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
    if not isinstance(models,list) or not models or any(not isinstance(m,str) or not m.strip() for m in models):raise ValueError('请填写文本及视觉模型名称')
    home=provider_home();home.mkdir(parents=True,exist_ok=True);path=home/'config.toml'
    config=tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    key_name='WORKBENCH_'+name.upper().replace('-','_')+'_KEY';secret=body.get('api_key','')
    if not isinstance(secret,str) or any(c in secret for c in '\r\n\0'):raise ValueError('授权格式无效')
    keys=dotenv_values(home/'.env')
    if secret:keys[key_name]=secret
    if not keys.get(key_name):raise ValueError('首次配置需要填写授权密钥')
    provider={'name':name,'kind':'openai','base_url':base,'api_key_env':key_name,'models':list(dict.fromkeys(models)),'default':models[0]}
    config['providers']=[p for p in config.get('providers',[]) if p.get('name')!=name]+[provider]
    config['default_model']=name+'/'+models[0]
    # Preserve existing non-provider settings, including native client permissions and plugins.
    path.write_text('\n'.join(json.dumps(k)+' = '+_toml(v) for k,v in config.items()),encoding='utf-8')
    secrets=home/'.env';secrets.write_text('\n'.join(k+'='+json.dumps(v) for k,v in keys.items() if isinstance(v,str)),encoding='utf-8')
    if os.name=='nt':
        user=subprocess.check_output(['whoami'],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
        result=subprocess.run(['icacls',str(secrets),'/inheritance:r','/grant:r',user+':F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise ValueError('授权文件访问权限设置失败')
    conversation.close()
    return read()
