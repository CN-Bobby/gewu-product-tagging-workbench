import json
from pathlib import Path
from sqlalchemy import select
from backend.build_info import VERSION, build_info
from backend.db import BatchJob, Session
from backend import text_facts

def test_version_is_consistent():
    root=Path(__file__).resolve().parents[1]
    assert VERSION==json.loads((root/'VERSION.json').read_text())['version']
    assert VERSION==json.loads((root/'frontend/package.json').read_text())['version']
    from backend.app import health
    assert health()['version']==VERSION
    assert len(health()['source_sha256'])==64

def test_task_build_is_frozen_at_creation(dataset):
    job=text_facts.prepare(dataset['id'])
    with Session() as s:
        saved=s.get(BatchJob,job['id'])
        assert saved.spec['application_build']==build_info()
        assert saved.spec['application_build']['version']==VERSION


def test_packaged_identity_does_not_duplicate_file_inventory(tmp_path,monkeypatch):
    import hashlib
    from backend import build_info as module
    manifest={'version':VERSION,'commit':'test-build','files':{'runtime/file'+str(i):'f'*64 for i in range(3000)}}
    raw=json.dumps(manifest).encode();(tmp_path/'build-manifest.json').write_bytes(raw)
    monkeypatch.setattr(module,'ROOT',tmp_path);module.build_info.cache_clear()
    try:
        identity=module.build_info()
        assert 'files' not in identity and len(json.dumps(identity))<1000
        assert identity['manifest_sha256']==hashlib.sha256(raw).hexdigest()
    finally:module.build_info.cache_clear()


def test_desktop_launcher_keeps_runtime_cache_out_of_install_directory():
    root=Path(__file__).resolve().parents[1]
    source=(root/'scripts'/'DesktopLauncher.cs').read_text(encoding='utf-8')
    assert 'start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1"' in source
    assert '!Occupied(port)&&StorageReleased(data)' in source
