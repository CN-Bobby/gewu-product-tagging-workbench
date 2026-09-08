import json
from pathlib import Path
import pytest
from backend import runtime_isolation,model_settings,storage_management,draft
from backend.db import *


def source(tmp_path,monkeypatch):
    home=tmp_path/'providers';home.mkdir()
    (home/'config.toml').write_text('default_model="test/text"\n[[providers]]\nname="test"\nkind="openai"\nbase_url="https://test.invalid/v1"\napi_key_env="TEST_KEY"\nmodels=["text"]\n[[plugins]]\nname="unrelated"\ncommand="never"\n',encoding='utf-8')
    (home/'.env').write_text('TEST_KEY="synthetic-test-only"\nUNRELATED="not-copied"',encoding='utf-8')
    monkeypatch.setenv('TAG_PROVIDER_HOME',str(home));return home


def test_homes_do_not_share_sessions_memory_or_plugins(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    a=runtime_isolation.environment('test_a');b=runtime_isolation.environment('test_b')
    first=Path(a['REASONIX_HOME']);second=Path(b['REASONIX_HOME'])
    assert first!=second and a['TAG_PROVIDER_HOME']==str(home)
    (first/'memory').mkdir();(first/'memory'/'test').write_text('dataset A only')
    assert not (second/'memory').exists()
    assert 'unrelated' not in (first/'config.toml').read_text()
    assert 'UNRELATED' not in (first/'.env').read_text()
    with pytest.raises(ValueError):runtime_isolation.environment('../escape')


def test_missing_legacy_session_blocks_instead_of_replacing(tmp_path,monkeypatch):
    source(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='迁移阻塞'):runtime_isolation.environment('legacy','missing-session')


def test_session_copy_backed_up_and_original_preserved(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch);sessions=home/'sessions';sessions.mkdir()
    (sessions/'existing.acp.json').write_text('{"sessionId":"existing"}')
    (sessions/'existing.jsonl').write_text('original transcript')
    (sessions/'existing.jsonl.lock').write_text('lock not copied')
    env=runtime_isolation.environment('legacy_copy','existing');target=Path(env['REASONIX_HOME'])
    assert (target/'sessions/existing.jsonl').read_text()=='original transcript'
    assert not (target/'sessions/existing.jsonl.lock').exists()
    report=json.loads((target/'migration.json').read_text());assert Path(report['backup']).is_dir()
    runtime_isolation.mark_resumed('legacy_copy','existing')
    assert json.loads((target/'migration.json').read_text())['status']=='acp_resume_verified'
    assert (sessions/'existing.jsonl').read_text()=='original transcript'


def test_model_config_api_never_returns_secret(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    result=model_settings.save({'name':'test','base_url':'https://test.invalid/v1','models':['text','test-vision'],'api_key':'synthetic-new-secret'})
    assert 'synthetic-new-secret' not in json.dumps(result)
    assert model_settings.defaults()==('test/text','test/test-vision')
    assert 'unrelated' in (home/'config.toml').read_text()
    with pytest.raises(ValueError):model_settings.save({'name':'test','base_url':'http://unsafe','models':['text']})


def test_cache_cleanup_preserves_authoritative_records_and_images(dataset):
    before=draft.sheet(dataset['id'])
    with Session.begin() as s:s.add(VisionCache(fingerprint='test-only',content={'observation':'disposable'}));s.add(TextCache(fingerprint='test-text',content={'facts':[]}))
    storage_management.clear()
    assert draft.sheet(dataset['id'])==before
    assert Path(dataset['original_file']).is_file()
    assert all(Path(a['path']).is_file() for r in before['rows'] for a in r['assets'] if a['path'])
