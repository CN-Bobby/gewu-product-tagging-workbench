"""关键词调研用到的两处凭据。

模型凭据**只解析、不复制、不代填**：走工作台既有的 provider 规则（与 vision.py 的
provider_config 同一套原语），因此用户在模型设置里配一次，两个工具都用同一把 key。

SellerSprite MCP 的 secret-key 是一条新的外部服务密钥，落在工作台自己的数据目录：
<STORAGE>/keyword-research/credentials.env。**不放 Reasonix 的 config.toml**——那个文件被
model_settings.read() 无条件遍历（不按 kind 过滤，model_settings.py:164）并在变更时回写，
塞一条没有模型的 provider 会在模型设置页多出一行空 provider，还会被别的代码路径反复重写。
"""
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..runtime_isolation import CredentialUnavailable

SECRET_NAME = 'SELLERSPRITE_MCP_SECRET_KEY'
DEFAULT_MCP_URL = 'https://mcp.sellersprite.com/mcp'


def _storage_dir():
    from ..db import STORAGE
    return STORAGE / 'keyword-research'


def secret_path():
    return _storage_dir() / 'credentials.env'


def read_secret():
    path = secret_path()
    if not path.exists():
        return ''
    for line in path.read_text(encoding='utf-8').splitlines():
        name, sep, value = line.partition('=')
        if sep and name.strip() == SECRET_NAME:
            return value.strip().strip('"').strip("'")
    return ''


def status():
    """只报是否已配置与何时写入：密钥本身不回显，任何部分都不回显。"""
    path = secret_path()
    configured = bool(read_secret())
    stamp = None
    if configured:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return {'configured': configured, 'updated_at': stamp}


def save_secret(value):
    """只写这一条。密钥不进日志、不进运行记录。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请填写 SellerSprite MCP 密钥')
    secret = value.strip()
    if len(secret) > 500 or re.search(r'[\r\n]', secret):
        raise ValueError('密钥格式非法')
    path = secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.env.tmp')
    tmp.write_text(SECRET_NAME + '=' + secret + '\n', encoding='utf-8')
    os.replace(tmp, path)
    return status()


def clear_secret():
    path = secret_path()
    if path.exists():
        path.unlink()
    return status()


def mcp_endpoint():
    """完整端点：地址 + URL 编码后的 secret-key 查询参数（SellerSprite 的鉴权方式）。"""
    secret = read_secret()
    if not secret:
        raise CredentialUnavailable('尚未配置 SellerSprite MCP 密钥，请在关键词调研页面填写')
    return DEFAULT_MCP_URL.rstrip('/') + '?secret-key=' + quote(secret, safe='')


def model_target(model_ref=None, credential_id=None):
    """解析出 (base_url, key, model)。模型引用形如 provider/model。"""
    import tomllib
    from dotenv import dotenv_values
    from ..model_settings import _canonical_config, local_model_name
    from ..runtime_isolation import provider_home, _effective_credential

    home = provider_home()
    path = home / 'config.toml'
    if not path.exists():
        raise CredentialUnavailable('尚未配置模型服务，请先在模型设置里配置一个服务')
    config, _ = _canonical_config(tomllib.loads(path.read_text(encoding='utf-8-sig')))
    providers = config.get('providers') or []
    if not providers:
        raise CredentialUnavailable('尚未配置模型服务，请先在模型设置里配置一个服务')

    reference = str(model_ref or '').strip() or str(config.get('default_model') or '')
    name, sep, model = reference.partition('/')
    if not name:
        name = str(providers[0].get('name') or '')
    provider = next((p for p in providers if p.get('name') == name), None)
    if provider is None:
        raise CredentialUnavailable(f'模型服务 {name} 未在模型设置中配置')
    if not sep or not model:
        model = (provider.get('default') or provider.get('text_model')
                 or (provider.get('models') or [''])[0])
    model = local_model_name(name, model)
    if not model:
        raise CredentialUnavailable('模型引用缺少具体模型名称')
    declared_models = [local_model_name(name, value) for value in provider.get('models', [])]
    if declared_models and model not in declared_models:
        raise CredentialUnavailable(f'模型 {reference} 不在服务的已配置模型列表中')
    if provider.get('kind', 'openai') != 'openai':
        raise CredentialUnavailable('关键词调研只支持 OpenAI 兼容的模型服务')

    # 与一次启动同一条规则：显式选择已失效或被停用就报错，绝不悄悄回落到当前的 primary。
    selected = _effective_credential(provider, credential_id)
    key_name = (selected or {}).get('api_key_env') or provider.get('api_key_env')
    key = (os.environ.get(key_name) or dotenv_values(home / '.env').get(key_name)) if key_name else None
    if not key:
        raise CredentialUnavailable('模型服务尚未保存有效授权，请先在模型设置里配置')
    base = str(provider.get('base_url') or '').rstrip('/')
    if not base.startswith('https://'):
        raise CredentialUnavailable('模型服务需使用已配置的 HTTPS 端点')
    return base, key, model


def model_selection(model_ref=None, credential_id=None):
    """Resolve the stable model/key identity used by plan fingerprints and run snapshots.

    The returned object has identifiers only. Secrets remain in the existing provider .env file.
    """
    import tomllib
    from ..model_settings import _canonical_config, defaults, normalize_reference
    from ..runtime_isolation import _effective_credential, provider_home

    home = provider_home()
    config_path = home / 'config.toml'
    if not config_path.exists():
        return {'model_ref': str(model_ref or defaults()[0]), 'credential_id': credential_id}
    config, _ = _canonical_config(tomllib.loads(config_path.read_text(encoding='utf-8-sig')))
    reference = normalize_reference(str(model_ref or config.get('default_model') or defaults()[0]))
    provider_name = reference.partition('/')[0]
    provider = next((p for p in config.get('providers', []) if p.get('name') == provider_name), None)
    if provider is None:
        raise CredentialUnavailable(f'模型服务 {provider_name} 未在模型设置中配置')
    local = reference.partition('/')[2]
    declared_models = [str(x) for x in provider.get('models', [])]
    if local and declared_models and local not in declared_models:
        raise CredentialUnavailable(f'模型 {reference} 不在服务的已配置模型列表中')
    selected = _effective_credential(provider, credential_id)
    return {'model_ref': reference, 'credential_id': (selected or {}).get('id')}
