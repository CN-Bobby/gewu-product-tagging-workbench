import json
import sys
import types

import pytest

from backend import credential_vault as vault


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    path = tmp_path / 'outside-data' / 'credentials.dpapi'
    monkeypatch.setattr(vault, '_vault_path', lambda: path)
    # Test-only reversible cipher; production implementations use Windows DPAPI.
    monkeypatch.setattr(vault, '_protect', lambda data: b'ENC:' + data[::-1])
    monkeypatch.setattr(vault, '_unprotect', lambda data: data[4:][::-1] if data.startswith(b'ENC:') else (_ for _ in ()).throw(ValueError('bad envelope')))
    monkeypatch.setenv('GEWU_PROFILE', 'dev')
    return path


def test_profile_scopes_share_dev_test_and_isolate_prod(fake_vault):
    vault.set_secret('sellersprite', 'mcp_secret_key', 'fixture-key', 'dev')
    assert vault.get_secret('sellersprite', 'mcp_secret_key', 'test') == 'fixture-key'
    assert vault.get_secret('sellersprite', 'mcp_secret_key', 'prod') is None
    vault.set_secret('sellersprite', 'mcp_secret_key', 'prod-key', 'prod')
    assert vault.get_secret('sellersprite', 'mcp_secret_key', 'prod') == 'prod-key'
    assert vault.credential_status('test') == {'profile': 'dev-test', 'model': False, 'sellersprite': True, 'sorftime': False}
    assert vault.credential_status('prod')['sellersprite'] is True


def test_vault_file_contains_only_encrypted_bytes(fake_vault):
    marker = 'never-plaintext-in-file'
    vault.set_secret('model', 'WORKBENCH_TEST_KEY', marker)
    assert fake_vault.is_file()
    assert marker.encode() not in fake_vault.read_bytes()
    assert vault.get_secret('model', 'WORKBENCH_TEST_KEY') == marker


def test_profile_selection_uses_launcher_environment(fake_vault, monkeypatch):
    monkeypatch.setenv('GEWU_PROFILE', 'test')
    vault.set_secret('sorftime', 'api_key', 'test-value')
    assert vault.get_secret('sorftime', 'api_key', 'dev') == 'test-value'
    monkeypatch.setenv('GEWU_PROFILE', 'prod')
    assert vault.get_secret('sorftime', 'api_key') is None


def test_invalid_profiles_and_secret_values_are_rejected(fake_vault):
    with pytest.raises(ValueError):
        vault.profile_scope('staging')
    with pytest.raises(ValueError):
        vault.set_secret('model', 'key', 'bad\nvalue')


def test_sellersprite_and_sorftime_adapters_use_vault_only(fake_vault, tmp_path, monkeypatch):
    if 'dotenv' not in sys.modules:
        dotenv = types.ModuleType('dotenv')
        dotenv.dotenv_values = lambda *args, **kwargs: {}
        monkeypatch.setitem(sys.modules, 'dotenv', dotenv)
    from backend import db
    from backend.keyword_research import credentials as seller
    from backend.xunlongjue import settings as sorftime

    monkeypatch.setattr(db, 'STORAGE', tmp_path / 'business-data')
    assert seller.status() == {'configured': False}
    seller.save_secret('ss-test-secret')
    assert seller.status() == {'configured': True}
    assert 'ss-test-secret' not in json.dumps(seller.status())
    assert seller.read_secret() == 'ss-test-secret'
    assert not seller.secret_path().exists()

    assert sorftime.status()['key_configured'] is False
    sorftime.save('https://mcp.example.test/api', 'sf-test-secret')
    assert sorftime.connection() == ('https://mcp.example.test/api', 'sf-test-secret')
    stored = sorftime._path().read_text(encoding='utf-8')
    assert 'sf-test-secret' not in stored
    sorftime.clear_key()
    assert sorftime.status()['key_configured'] is False


def test_model_target_ignores_ambient_environment_key(fake_vault, tmp_path, monkeypatch):
    if 'dotenv' not in sys.modules:
        dotenv = types.ModuleType('dotenv')
        dotenv.dotenv_values = lambda *args, **kwargs: {}
        monkeypatch.setitem(sys.modules, 'dotenv', dotenv)
    from backend import model_settings
    from backend.keyword_research import credentials
    import backend.runtime_isolation as isolation

    home = tmp_path / 'providers'
    home.mkdir()
    model_settings._write_config(home / 'config.toml', {
        'default_model': 'fake/m1',
        'providers': [{
            'name': 'fake', 'kind': 'openai', 'base_url': 'https://api.example.test/v1',
            'models': ['m1'], 'default': 'm1', 'api_key_env': 'FAKE_KEY',
            'credentials': [{'id': 'main', 'api_key_env': 'FAKE_KEY', 'enabled': True}],
            'primary_credential_id': 'main',
        }],
    })
    monkeypatch.setattr(isolation, 'provider_home', lambda: home)
    monkeypatch.setenv('FAKE_KEY', 'ambient-wrong-key')
    vault.set_secret('model', 'FAKE_KEY', 'vault-selected-key')
    _, key, model = credentials.model_target('fake/m1', 'main')
    assert (key, model) == ('vault-selected-key', 'm1')


def test_presence_status_does_not_expose_vault_identifiers(fake_vault):
    vault.set_secret('model', 'SENSITIVE_ENV_NAME', 'secret')
    response = json.dumps(vault.credential_status())
    assert 'SENSITIVE_ENV_NAME' not in response
    assert 'secret' not in response
