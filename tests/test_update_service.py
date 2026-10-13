import json
import urllib.error

import pytest

from backend import update_service


SHA256 = 'a' * 64


class Response:
    def __init__(self, body: bytes, url: str = 'https://api.github.com/mock'):
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url


def release(version='0.5.0-beta.8', *, digest='sha256:' + SHA256, assets=None, **overrides):
    expected = f'GeWuWorkbench-{version}-portable.zip'
    result = {
        'draft': False,
        'published_at': '2026-10-10T12:00:00Z',
        'tag_name': 'v' + version,
        'html_url': f'https://github.com/CN-Bobby/gewu-product-tagging-workbench/releases/tag/v{version}',
        'body': '候选发布\n\n修复更新流程。',
        'assets': assets if assets is not None else [{
            'name': expected,
            'browser_download_url': f'https://github.com/CN-Bobby/gewu-product-tagging-workbench/releases/download/v{version}/{expected}',
            'digest': digest,
        }],
    }
    result.update(overrides)
    return result


def mock_api(monkeypatch, payload):
    calls = []

    def fake_open(request, timeout, *, allowed_redirect_hosts):
        url = request.full_url if hasattr(request, 'full_url') else request
        calls.append((url, timeout))
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode('utf-8')
        return Response(body)

    monkeypatch.setattr(update_service, '_open', fake_open)
    return calls


def test_checks_latest_published_release_and_ignores_drafts(monkeypatch):
    calls = mock_api(monkeypatch, [
        release('0.5.0-beta.9', draft=True, published_at=None),
        release('0.5.0-beta.8'),
        release('0.5.0-beta.7', published_at='2026-10-09T12:00:00Z'),
    ])
    result = update_service.check_for_update('0.5.0-beta.7+abc123')
    assert result == {
        'current_version': '0.5.0-beta.7+abc123',
        'latest_version': '0.5.0-beta.8',
        'update_available': True,
        'download_url': 'https://github.com/CN-Bobby/gewu-product-tagging-workbench/releases/download/v0.5.0-beta.8/GeWuWorkbench-0.5.0-beta.8-portable.zip',
        'sha256': SHA256,
        'release_url': 'https://github.com/CN-Bobby/gewu-product-tagging-workbench/releases/tag/v0.5.0-beta.8',
        'release_notes_summary': '候选发布 修复更新流程。',
    }
    assert len(calls) == 1
    assert '/repos/CN-Bobby/gewu-product-tagging-workbench/releases?' in calls[0][0]
    assert calls[0][1] == update_service.DEFAULT_TIMEOUT_SECONDS


def test_selects_highest_semver_not_most_recent_publish_time(monkeypatch):
    mock_api(monkeypatch, [
        release('0.5.0-beta.8', published_at='2026-10-12T12:00:00Z'),
        release('0.5.0-beta.10', published_at='2026-10-09T12:00:00Z'),
    ])
    result = update_service.check_for_update('0.5.0-beta.8')
    assert result['latest_version'] == '0.5.0-beta.10'
    assert result['update_available'] is True


def test_no_update_is_reported_without_fetching_asset(monkeypatch):
    mock_api(monkeypatch, [release('0.5.0-beta.7')])
    result = update_service.check_for_update('0.5.0-beta.7')
    assert result['latest_version'] == '0.5.0-beta.7'
    assert result['update_available'] is False


def test_repository_can_be_overridden_for_local_tests(monkeypatch):
    monkeypatch.setenv('GEWU_UPDATE_REPOSITORY', 'test-owner/test-releases')
    calls = mock_api(monkeypatch, [release()])
    update_service.check_for_update('0.5.0-beta.7')
    assert '/repos/test-owner/test-releases/releases?' in calls[0][0]


def test_404_fails_closed(monkeypatch):
    mock_api(monkeypatch, urllib.error.HTTPError('https://api.github.com/mock', 404, 'missing', {}, None))
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'repository_not_found'


def test_invalid_json_fails_closed(monkeypatch):
    mock_api(monkeypatch, b'{broken')
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'invalid_release_response'


@pytest.mark.parametrize('assets', [
    [],
    [
        {'name': 'GeWuWorkbench-0.5.0-beta.8-portable.zip',
         'browser_download_url': 'https://github.com/example/a.zip', 'digest': 'sha256:' + SHA256},
        {'name': 'GeWuWorkbench-0.5.0-beta.7-portable.zip',
         'browser_download_url': 'https://github.com/example/b.zip', 'digest': 'sha256:' + SHA256},
    ],
])
def test_missing_or_multiple_portable_assets_fail_closed(monkeypatch, assets):
    mock_api(monkeypatch, [release(assets=assets)])
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'portable_asset_missing'


def test_invalid_api_digest_fails_closed_even_if_sidecar_exists(monkeypatch):
    version = '0.5.0-beta.8'
    package = f'GeWuWorkbench-{version}-portable.zip'
    assets = [
        {'name': package, 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package,
         'digest': 'sha256:bad'},
        {'name': package + '.sha256', 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package + '.sha256'},
    ]
    mock_api(monkeypatch, [release(version, assets=assets)])
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'invalid_sha256'


def test_missing_digest_uses_same_release_sha256_sidecar(monkeypatch):
    version = '0.5.0-beta.8'
    package = f'GeWuWorkbench-{version}-portable.zip'
    sidecar_url = 'https://objects.githubusercontent.com/releases/' + package + '.sha256'
    assets = [
        {'name': package, 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package},
        {'name': package + '.sha256', 'browser_download_url': sidecar_url},
    ]
    calls = []

    def fake_open(request, timeout, *, allowed_redirect_hosts):
        url = request.full_url if hasattr(request, 'full_url') else request
        calls.append(url)
        if 'api.github.com' in url:
            return Response(json.dumps([release(version, assets=assets)]).encode(), url)
        return Response((SHA256 + '  ' + package + '\n').encode(), url)

    monkeypatch.setattr(update_service, '_open', fake_open)
    result = update_service.check_for_update('0.5.0-beta.7')
    assert result['sha256'] == SHA256
    assert calls[1] == sidecar_url


def test_malformed_sidecar_digest_fails_closed(monkeypatch):
    version = '0.5.0-beta.8'
    package = f'GeWuWorkbench-{version}-portable.zip'
    assets = [
        {'name': package, 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package},
        {'name': package + '.sha256', 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package + '.sha256'},
    ]

    def fake_open(request, timeout, *, allowed_redirect_hosts):
        url = request.full_url if hasattr(request, 'full_url') else request
        if 'api.github.com' in url:
            return Response(json.dumps([release(version, assets=assets)]).encode(), url)
        return Response(b'nope  ' + package.encode(), url)

    monkeypatch.setattr(update_service, '_open', fake_open)
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'invalid_sha256'


@pytest.mark.parametrize('url', [
    'http://github.com/o/r/releases/download/a.zip',
    'https://evil.example/download/a.zip',
    'https://github.com.evil.example/o/r/releases/a.zip',
    'https://user@github.com/o/r/releases/a.zip',
])
def test_unsafe_portable_asset_urls_fail_closed(monkeypatch, url):
    item = release()
    item['assets'][0]['browser_download_url'] = url
    mock_api(monkeypatch, [item])
    with pytest.raises(update_service.UpdateCheckError) as caught:
        update_service.check_for_update('0.5.0-beta.7')
    assert caught.value.code == 'unsafe_asset_url'


def test_sidecar_redirect_to_github_release_asset_host_is_allowed(monkeypatch):
    version = '0.5.0-beta.8'
    package = f'GeWuWorkbench-{version}-portable.zip'
    sidecar_url = 'https://github.com/o/r/releases/download/x/' + package + '.sha256'
    assets = [
        {'name': package, 'browser_download_url': 'https://github.com/o/r/releases/download/x/' + package},
        {'name': package + '.sha256', 'browser_download_url': sidecar_url},
    ]

    allowed_hosts = set()

    def fake_open(request, timeout, *, allowed_redirect_hosts):
        url = request.full_url if hasattr(request, 'full_url') else request
        if 'api.github.com' in url:
            return Response(json.dumps([release(version, assets=assets)]).encode(), url)
        allowed_hosts.update(allowed_redirect_hosts)
        return Response(SHA256.encode(), 'https://release-assets.githubusercontent.com/redirect')

    monkeypatch.setattr(update_service, '_open', fake_open)
    result = update_service.check_for_update('0.5.0-beta.7')
    assert result['sha256'] == SHA256
    assert 'release-assets.githubusercontent.com' in allowed_hosts


@pytest.mark.parametrize('redirect_url', [
    'https://user@github.com/o/r/releases/file',
    'https://github.com:8443/o/r/releases/file',
])
def test_redirect_rejects_userinfo_and_nonstandard_ports(redirect_url):
    handler = update_service._AllowlistRedirectHandler({'github.com', 'objects.githubusercontent.com'})
    request = update_service.urllib.request.Request('https://github.com/o/r/releases/file')
    with pytest.raises(update_service.UpdateCheckError) as caught:
        handler.redirect_request(request, None, 302, 'Found', {}, redirect_url)
    assert caught.value.code == 'unsafe_redirect'
