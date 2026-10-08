"""Offline policy and runtime identity contracts for DEV/TEST."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, profile='test'):
    monkeypatch.setenv('GEWU_PROFILE', profile)
    from backend import app as app_module
    monkeypatch.setattr(app_module.external_effects,'effects_allowed',
                        lambda service=None: app_module.external_effects.profile() == 'prod')
    return app_module, TestClient(app_module.app)


def test_unknown_and_missing_profiles_fail_closed(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects,'effects_allowed',
                        lambda service=None: external_effects.profile() == 'prod')

    monkeypatch.delenv('GEWU_PROFILE', raising=False)
    assert external_effects.profile() == 'dev'
    assert not external_effects.effects_allowed()
    monkeypatch.setenv('GEWU_PROFILE', 'typo')
    assert external_effects.profile() == 'dev'
    assert not external_effects.effects_allowed()
    monkeypatch.setenv('GEWU_PROFILE', 'prod')
    assert external_effects.effects_allowed('model_api')


def test_offline_api_rejects_costly_jobs_but_keeps_local_preview(monkeypatch):
    app_module, client = _client(monkeypatch)
    with client:
        blocked = [
            ('post', '/api/agent/sessions', {}),
            ('post', '/api/agent/sessions/session/messages', {'text': 'test'}),
            ('post', '/api/amazon-scraper/jobs', {'input': 'B000000001'}),
            ('post', '/api/amazon-scraper/jobs/job/retry', {}),
            ('post', '/api/amazon-reviews/jobs', {}),
            ('post', '/api/amazon-reviews/jobs/job/retry', {}),
            ('post', '/api/amazon-voc/projects/project/runs', {}),
            ('post', '/api/amazon-voc/from-amazon-reviews/job', {'confirmed_reviewed': True}),
            ('post', '/api/amazon-voc/runs/run/retry', {}),
            ('post', '/api/keyword-research/plan', {}),
            ('post', '/api/keyword-research/runs', {}),
            ('post', '/api/keyword-research/queue', {}),
            ('post', '/api/keyword-research/queue/continue', {}),
            ('post', '/api/xunlongjue/runs', {}),
            ('post', '/api/xunlongjue/runs/bulk', {}),
            ('post', '/api/xunlongjue/runs/run/resume', {}),
            ('post', '/api/drafts/dataset/fields/suggest', {}),
            ('post', '/api/drafts/dataset/profile/observe', {}),
            ('post', '/api/drafts/dataset/text-records/extract', {}),
            ('post', '/api/drafts/dataset/knowledge', {'query': 'product terminology'}),
            ('post', '/api/jobs/job/resume', {}),
            ('post', '/api/datasets/dataset/images', {}),
            ('post', '/api/runs/run/resume', {}),
        ]
        for method, path, body in blocked:
            response = getattr(client, method)(path, json=body)
            assert response.status_code == 403, (path, response.status_code, response.text)
            assert response.json()['detail']['code'] == 'external_effects_disabled'

        # Pure parsing is useful in acceptance work and does not fetch a product page.
        preview = client.post('/api/amazon-scraper/preview', json={'input': 'B000000001'})
        assert preview.status_code == 200
        assert preview.json()['items'][0]['asin'] == 'B000000001'


def test_runtime_status_is_presence_only_and_identifies_isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv('TAG_STORAGE', str(tmp_path / 'isolated-data'))
    app_module, client = _client(monkeypatch)
    from backend import credential_vault
    from backend.db import STORAGE
    monkeypatch.setattr(credential_vault, 'credential_status', lambda profile=None: {
        'profile': 'dev-test', 'model': True, 'sellersprite': False, 'sorftime': False,
    })
    secret_marker = 'DO_NOT_RETURN_THIS_SECRET'
    with client:
        status = client.get('/api/runtime/status')
        assert status.status_code == 200
        value = status.json()
        assert value['profile'] == 'test'
        assert value['external_effects']['policy'] == 'offline'
        assert value['credentials']['model'] is True
        assert value['data']['path'] == str(STORAGE)
        assert 'commit' in value['source'] and 'dirty' in value['source']
        assert secret_marker not in json.dumps(value)


def test_runtime_status_reads_windows_utf8_report_without_exposing_report_contents(monkeypatch, tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir()
    (reports / 'offline-latest.json').write_bytes(b'\xef\xbb\xbf' + json.dumps({
        'status': 'passed', 'build_id': '0.5.0-beta.7+abc123', 'commit': 'abc123',
        'fixture_version': 'project-library-v1', 'api_key': 'NEVER_RETURN_THIS_VALUE',
    }).encode('utf-8'))
    monkeypatch.setenv('GEWU_REPORTS_DIR', str(reports))
    app_module, client = _client(monkeypatch)
    with client:
        value = client.get('/api/runtime/status').json()
    assert value['latest_test_report']['status'] == 'passed'
    assert value['latest_test_report']['fixture_version'] == 'project-library-v1'
    assert 'NEVER_RETURN_THIS_VALUE' not in json.dumps(value)


def test_test_profile_never_recovers_queued_external_jobs(monkeypatch):
    app_module, client = _client(monkeypatch)
    recovered = []

    def mark(name):
        return lambda *args, **kwargs: recovered.append(name)

    for owner, names in [
        (app_module.jobs, ('recover',)), (app_module.batch, ('recover',)),
        (app_module.keyword_registry, ('recover',)), (app_module.amazon_scraper, ('recover',)),
        (app_module.amazon_reviews, ('recover',)), (app_module, ('amazon_voc_recover',)),
        (app_module, ('xunlongjue_start', 'xunlongjue_recover')),
    ]:
        for name in names:
            monkeypatch.setattr(owner, name, mark(name))

    with client:
        assert client.get('/api/health').status_code == 200
    assert recovered == []


def test_offline_worker_boundary_refuses_direct_amazon_fetch(monkeypatch):
    monkeypatch.setenv('GEWU_PROFILE', 'test')
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'effects_allowed',
                        lambda service=None: external_effects.profile() == 'prod')
    from backend.amazon_scraper_worker import _fetch
    from backend.external_effects import ExternalEffectsDisabled
    with pytest.raises(ExternalEffectsDisabled):
        _fetch({'item': {'asin': 'B000000001'}})


def test_offline_import_does_not_queue_automatic_model_extraction(monkeypatch, tmp_path):
    app_module, client = _client(monkeypatch)
    monkeypatch.setattr(app_module.importer, 'import_file', lambda *args, **kwargs: {'id': 'fixture-dataset'})
    monkeypatch.setattr(app_module.worksheet_query, 'profile', lambda *args, **kwargs: {'fields': []})
    monkeypatch.setattr(app_module.text_facts, 'prepare', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('model task queued')))
    with client:
        response = client.post('/api/import', files={'file': ('fixture.xlsx', b'fixture-bytes')})
    assert response.status_code == 200, response.text
    assert response.json()['text_job_id'] is None
