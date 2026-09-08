"""Manual-update notices from a real publisher manifest; never install automatically."""
import json,re
from .db import ROOT
from .build_info import VERSION
from .knowledge import fetch,check_url

def ordering(version):
    m=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?',version)
    if not m:raise ValueError('发布清单版本号无效')
    return tuple(map(int,m.group(1,2,3)))+({'alpha':0,'beta':1,'rc':2,None:3}[m[4]],int(m[5] or 0))

def check():
    manifest=json.loads((ROOT/'VERSION.json').read_text(encoding='utf-8'));url=manifest.get('release_manifest_url')
    if not url:return {'configured':False,'current_version':VERSION,'status':'更新入口待配置'}
    try:
        remote=json.loads(fetch(url));latest=remote['version'];ordering(latest)
        if remote.get('installer_url'):check_url(remote['installer_url'])
        return {'configured':True,'current_version':VERSION,'latest_version':latest,
            'available':ordering(latest)>ordering(VERSION),'source':url,
            'notes':str(remote.get('notes',''))[:4000],'contact':str(remote.get('contact',''))[:500],
            'installer_url':remote.get('installer_url'),'sha256':remote.get('sha256'),
            'status':'请联系发布方获取安装包' if ordering(latest)>ordering(VERSION) else '当前已是最新版本'}
    except Exception as e:return {'configured':True,'current_version':VERSION,'status':'更新查询失败','error':str(e)[:300]}
