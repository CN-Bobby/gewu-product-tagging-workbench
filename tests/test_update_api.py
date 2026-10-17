from fastapi.testclient import TestClient

from backend.app import app
from backend import external_effects, update_service


def test_manual_update_route_returns_metadata_without_downloading(monkeypatch):
    expected = {
        'current_version': '0.5.0-beta.7',
        'latest_version': '0.5.0-beta.8',
        'update_available': True,
        'download_url': 'https://github.com/owner/repo/releases/download/v0.5.0-beta.8/GeWuWorkbench-0.5.0-beta.8-portable.zip',
        'sha256': 'a' * 64,
        'release_url': 'https://github.com/owner/repo/releases/tag/v0.5.0-beta.8',
        'release_notes_summary': '更新说明',
    }
    monkeypatch.setattr(update_service, 'check_for_update', lambda: expected)
    with TestClient(app) as client:
        response = client.get('/api/updates/check')
    assert response.status_code == 200
    assert response.json() == expected


def test_manual_update_route_obeys_offline_policy(monkeypatch):
    monkeypatch.setattr(external_effects, 'effects_allowed', lambda _service=None: False)
    called = False

    def unexpected_call():
        nonlocal called
        called = True
        raise AssertionError('update lookup must not run while offline')

    monkeypatch.setattr(update_service, 'check_for_update', unexpected_call)
    with TestClient(app) as client:
        response = client.get('/api/updates/check')
    assert response.status_code == 403
    assert called is False


def test_manual_update_route_maps_github_failures_to_gateway_errors(monkeypatch):
    def fail():
        raise update_service.UpdateCheckError('repository_not_found', '更新仓库不存在')

    monkeypatch.setattr(update_service, 'check_for_update', fail)
    with TestClient(app) as client:
        response = client.get('/api/updates/check')
    assert response.status_code == 502
    assert response.json()['detail']['code'] == 'repository_not_found'
