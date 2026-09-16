"""Dataset-owned Reasonix state; shared provider authorization stays outside sessions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from dotenv import dotenv_values
from .db import STORAGE, now


def provider_home():
    return Path(os.environ.get('TAG_PROVIDER_HOME') or os.environ.get('REASONIX_HOME') or
                str(Path(os.environ.get('APPDATA', str(Path.home()/'.config')))/'reasonix')).resolve()


def _toml(value):
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k)+' = '+_toml(v) for k,v in value.items() if v is not None) + '}'
    if isinstance(value, list):return '['+', '.join(_toml(v) for v in value)+']'
    return json.dumps(value, ensure_ascii=False)


class CredentialUnavailable(ValueError):
    """No usable credential for the provider a launch must use.

    Raised before a runtime exists, so an unusable key can never surface later as a provider
    authentication failure. It keeps ``ValueError`` as its base because that is what a refused
    configuration already meant to every existing caller.
    """
    code='credential_unavailable'


def _declared_credentials(provider):
    """The credentials the provider declares, or ``None`` when it declares none at all.

    The distinction is the schema's own: a provider that never declared a credential list needs no
    key, while an empty list means every key of that provider was removed. That is why an explicitly
    empty list is a failure and an absent list is not.
    """
    declared=provider.get('credentials')
    return None if declared is None else [x for x in declared if isinstance(x,dict)]


def _effective_credential(provider,credential_id=None):
    """The credential one launch must use, or a failure naming why it cannot be used.

    An explicit choice that is missing, disabled or from another provider is refused instead of
    falling back to whatever is primary now: a silent fallback would bill a different key than the
    caller asked for.
    """
    name=provider.get('name')
    declared=_declared_credentials(provider)
    if declared is None and not provider.get('api_key_env'):return None
    credentials=declared or []
    if credential_id:
        chosen=next((x for x in credentials if x.get('id')==credential_id),None)
        if chosen is None:raise CredentialUnavailable(f'服务 {name} 中不存在所选 Key：{credential_id}')
    else:
        chosen=next((x for x in credentials if x.get('id')==provider.get('primary_credential_id')),None)
    if chosen is None:raise CredentialUnavailable(f'服务 {name} 没有可用的授权，请在模型设置中重新配置')
    if not chosen.get('enabled',True):raise CredentialUnavailable(f'服务 {name} 的所选 Key 已停用')
    return chosen


def credential_selection(model_ref,credential_id=None):
    """Resolve and validate the ``(provider, credential_id)`` one launch must use.

    Called once at a creation boundary so the runtime keeps the configuration it was created with.
    Resolving here — instead of letting ``environment()`` take "whatever is primary" — is what makes
    the frozen model, provider and credential a single snapshot rather than three separate guesses.
    """
    if not isinstance(model_ref,str) or not model_ref.strip():
        raise CredentialUnavailable('模型引用为空，请在模型设置中选择模型')
    path=provider_home()/'config.toml'
    if not path.is_file():raise CredentialUnavailable('请先在网页模型设置中配置服务与授权')
    from .model_settings import _canonical_config,normalize_reference
    config,_=_canonical_config(tomllib.loads(path.read_text(encoding='utf-8-sig')))
    name=normalize_reference(model_ref).partition('/')[0]
    provider=next((p for p in config.get('providers',[]) if p.get('name')==name),None)
    if provider is None:raise CredentialUnavailable('模型服务不存在：'+name)
    chosen=_effective_credential(provider,credential_id)
    return name,(chosen or {}).get('id')


def environment(dataset_id, session_id=None, credential_ids=None):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', dataset_id):raise ValueError('数据集目录标识无效')
    source=provider_home()
    home=STORAGE/'datasets'/dataset_id/'reasonix'
    home.mkdir(parents=True,exist_ok=True)
    if not (source/'config.toml').is_file():raise ValueError('请先在网页模型设置中配置服务与授权')
    config=tomllib.loads((source/'config.toml').read_text(encoding='utf-8-sig'))
    # The settings endpoint normally migrates legacy duplicated model
    # prefixes.  Canonicalize again here so direct API callers and a service
    # started during the first page load cannot launch a malformed project
    # configuration.
    from .model_settings import _canonical_config
    config,_=_canonical_config(config)
    # Only provider definitions are shared; never import global plugins, memory or permissions.
    providers=config.get('providers',[])
    credential_ids=credential_ids or {}
    # Every provider that declares a credential is checked here, whether or not one was selected:
    # a launch may never resolve an unusable key, and it may never quietly change which key it uses.
    for provider in providers:
        selected_id=credential_ids.get(provider.get('name'))
        chosen=_effective_credential(provider,selected_id)
        if chosen is None:continue
        if selected_id:
            provider['api_key_env']=chosen['api_key_env'];provider['primary_credential_id']=selected_id
    allowed={k:v for k,v in config.items() if k in ('providers','default_model','model')}
    (home/'config.toml').write_text('\n'.join(k+' = '+_toml(v) for k,v in allowed.items()),encoding='utf-8')
    if session_id and not (home/'sessions'/f'{session_id}.acp.json').is_file():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',session_id):raise ValueError('会话标识无效')
        files=list((source/'sessions').glob(session_id+'.*'))
        if not any(p.name==session_id+'.acp.json' for p in files):
            raise ValueError('会话隔离迁移阻塞：原会话文件不存在；保留原ID，不新建替代会话')
        backup=STORAGE/'backups'/'session-isolation'/dataset_id/session_id
        target=home/'sessions';target.mkdir(exist_ok=True);backup.mkdir(parents=True,exist_ok=True)
        hashes={}
        for path in files:
            if path.name.endswith(('.lock','.lease.json')):continue
            for dest in (backup/path.name,target/path.name):
                if dest.exists():continue
                if path.is_dir():shutil.copytree(path,dest,symlinks=True)
                else:shutil.copy2(path,dest)
            if path.is_file():hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        (home/'migration.json').write_text(json.dumps({'session_id':session_id,'source':str(source),
            'backup':str(backup),'copied_at':now(),'files_sha256':hashes,'status':'copied_pending_acp_resume'},
            ensure_ascii=False,indent=2),encoding='utf-8')
    env=dict(os.environ,REASONIX_HOME=str(home),TAG_PROVIDER_HOME=str(source))
    secrets=dotenv_values(source/'.env')
    credential_lines=[]
    for provider in providers:
        key=provider.get('api_key_env')
        if key and secrets.get(key):credential_lines.append(key+'='+json.dumps(secrets[key]))
    # 1.25.1 resolves provider keys exclusively from its home .env (ignores env overrides).
    # Keep only selected provider keys, refreshed on launch; never package or log this file.
    secret_path=home/'.env'
    secret_path.write_text('\n'.join(dict.fromkeys(credential_lines))+'\n',encoding='utf-8')
    if os.name=='nt':
        identity=subprocess.check_output(['whoami'],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
        result=subprocess.run(['icacls',str(secret_path),'/inheritance:r','/grant:r',identity+':F'],
                              capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise ValueError('无法限制数据集授权文件访问权限')
    return env
