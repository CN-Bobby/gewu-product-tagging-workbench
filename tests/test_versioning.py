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
