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


def environment(dataset_id, session_id=None):
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


def mark_resumed(dataset_id,session_id):
    manifest=STORAGE/'datasets'/dataset_id/'reasonix'/'migration.json'
    if manifest.exists():
        data=json.loads(manifest.read_text(encoding='utf-8'))
        if data['session_id']!=session_id:raise ValueError('迁移会话ID发生变化')
        data.update(status='acp_resume_verified',verified_at=now())
        manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
