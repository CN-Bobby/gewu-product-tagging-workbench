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
    monkeypatch.setattr(model_settings,'validate_models',lambda *args,**kwargs:{'status':'validated','validated_at':'test','text_model':args[2],'vision_model':args[3]})
    result=model_settings.save({'name':'test','base_url':'https://test.invalid/v1','models':['text','test-vision'],'api_key':'synthetic-new-secret'})
    assert 'synthetic-new-secret' not in json.dumps(result)
    assert model_settings.defaults()==('test/text','test/test-vision')
    assert 'unrelated' in (home/'config.toml').read_text()
    with pytest.raises(ValueError):model_settings.save({'name':'test','base_url':'http://unsafe','models':['text']})


def test_multiple_named_keys_are_preserved_and_selectable(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    monkeypatch.setattr(model_settings,'validate_models',lambda *args,**kwargs:{'status':'validated','validated_at':'test','text_model':args[2],'vision_model':args[3]})
    first=model_settings.save({'name':'test','base_url':'https://test.invalid/v1','text_model':'org/text','vision_model':'org/vision','api_key':'first-secret','credential_id':'first','credential_name':'工作 Key'})
    second=model_settings.save({'name':'test','base_url':'https://test.invalid/v1','text_model':'org/text','vision_model':'org/vision','api_key':'second-secret','credential_id':'second','credential_name':'备用 Key','set_default':False})
    provider=second['providers'][0]
    assert {x['name'] for x in provider['credentials']}=={'Default Key','工作 Key','备用 Key'}
    assert 'first-secret' not in json.dumps(second) and 'second-secret' not in json.dumps(second)
    model_settings.credential_action('test','first','primary')
    env=runtime_isolation.environment('selected_key',credential_ids={'test':'first'})
    isolated=Path(env['REASONIX_HOME'])
    assert 'first-secret' in (isolated/'.env').read_text(encoding='utf-8')
    assert 'second-secret' not in (isolated/'.env').read_text(encoding='utf-8')


def test_new_model_fields_are_saved_and_canonicalized(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    monkeypatch.setattr(model_settings,'validate_models',lambda *args,**kwargs:{'status':'validated','validated_at':'test','text_model':args[2],'vision_model':args[3]})
    result=model_settings.save({'name':'test','base_url':'https://test.invalid/v1',
        'text_model':'test/test/text','vision_model':'test/test/test-vision','api_key':'new-secret'})
    provider=result['providers'][0]
    assert provider['text_model']=='text' and provider['vision_model']=='test-vision'
    assert provider['model_refs']==['test/text','test/test-vision']
    assert provider['validation']['status']=='validated'


def test_validation_failure_keeps_previous_configuration(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    monkeypatch.setattr(model_settings,'validate_models',lambda *args,**kwargs:{'status':'validated','validated_at':'test','text_model':args[2],'vision_model':args[3]})
    model_settings.save({'name':'test','base_url':'https://test.invalid/v1','text_model':'text','vision_model':'test-vision','api_key':'original-secret'})
    before=(home/'config.toml').read_text(),(home/'.env').read_text()
    def fail(*args,**kwargs): raise ValueError('视觉模型验证失败（模型或接口不存在，HTTP 404）')
    monkeypatch.setattr(model_settings,'validate_models',fail)
    with pytest.raises(ValueError,match='视觉模型验证失败'):
        model_settings.save({'name':'test','base_url':'https://new.invalid/v1','text_model':'new-text','vision_model':'new-vision','api_key':'replacement-secret'})
    assert ((home/'config.toml').read_text(),(home/'.env').read_text())==before


def test_independent_validation_failure_is_saved_without_losing_configuration(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    validation={'status':'partial','overall':'partial','validated_at':'test','text_model':'text','vision_model':'vision',
        'text':{'status':'validated','code':None,'message':'验证通过'},
        'vision':{'status':'failed','code':'vision_unsupported','message':'模型不支持视觉输入'}}
    monkeypatch.setattr(model_settings,'validate_models',lambda *args,**kwargs:validation)
    result=model_settings.save({'name':'test','base_url':'https://test.invalid/v1','text_model':'text','vision_model':'vision','api_key':'saved-secret'})
    provider=result['providers'][0]
    assert provider['validation']['status']=='partial'
    assert provider['credentials'][-1]['validation']['vision']['code']=='vision_unsupported'
    assert 'saved-secret' not in json.dumps(result) and (home/'config.toml').exists() and (home/'.env').exists()


def test_legacy_qualified_model_names_are_migrated(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    (home/'config.toml').write_text(
        'default_model="test/test/text"\n'
        '[[providers]]\nname="test"\nkind="openai"\n'
        'base_url="https://test.invalid/v1"\napi_key_env="TEST_KEY"\n'
        'models=["test/text","test/test-vision"]\n'
        'default="test/test/text"\n',encoding='utf-8')
    result=model_settings.read()
    assert result['default_model']=='test/text'
    assert result['providers'][0]['models']==['text','test-vision']
    assert result['providers'][0]['model_refs']==['test/text','test/test-vision']
    assert model_settings.defaults()==('test/text','test/test-vision')
    saved=(home/'config.toml').read_text()
    assert '"models" = ["text", "test-vision"]' in saved
    assert '"default_model" = "test/text"' in saved


def test_provider_config_accepts_legacy_duplicate_reference(tmp_path,monkeypatch):
    home=source(tmp_path,monkeypatch)
    from backend.vision import provider_config
    endpoint,key,model=provider_config('test/test/text')
    assert endpoint=='https://test.invalid/v1/chat/completions'
    assert key=='synthetic-test-only'
    assert model=='text'


def test_cache_cleanup_preserves_authoritative_records_and_images(dataset):
    before=draft.sheet(dataset['id'])
    with Session.begin() as s:s.add(VisionCache(fingerprint='test-only',content={'observation':'disposable'}));s.add(TextCache(fingerprint='test-text',content={'facts':[]}))
    storage_management.clear()
    assert draft.sheet(dataset['id'])==before
    assert Path(dataset['original_file']).is_file()
    assert all(Path(a['path']).is_file() for r in before['rows'] for a in r['assets'] if a['path'])
