"""Read-only check for a published portable build on GitHub Releases.

This module reports release metadata only. It never downloads the portable package,
installs it, or changes application state.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .build_info import VERSION


DEFAULT_REPOSITORY = 'CN-Bobby/gewu-workbench-releases'
DEFAULT_TIMEOUT_SECONDS = 8
MAX_SIDECAR_BYTES = 4096
MAX_RELEASE_PAGES = 10
RELEASE_PAGE_SIZE = 100
_REPOSITORY_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_SEMVER_RE = re.compile(
    r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$'
)


class UpdateCheckError(RuntimeError):
    """A fail-closed update check error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _version_parts(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    if not isinstance(value, str):
        raise UpdateCheckError('invalid_version', '版本号格式无效')
    match = _SEMVER_RE.fullmatch(value.strip())
    if not match:
        raise UpdateCheckError('invalid_version', '版本号格式无效')
    pre = tuple(match.group(4).split('.')) if match.group(4) else None
    if pre and any(not part or (part.isdigit() and len(part) > 1 and part.startswith('0')) for part in pre):
        raise UpdateCheckError('invalid_version', '预发布版本号格式无效')
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), pre


def _is_newer(candidate: str, current: str) -> bool:
    candidate_parts = _version_parts(candidate)
    current_parts = _version_parts(current.split('+', 1)[0])
    if candidate_parts[:3] != current_parts[:3]:
        return candidate_parts[:3] > current_parts[:3]
    candidate_pre, current_pre = candidate_parts[3], current_parts[3]
    if candidate_pre is None:
        return current_pre is not None
    if current_pre is None:
        return False
    for left, right in zip(candidate_pre, current_pre):
        if left == right:
            continue
        if left.isdigit() and right.isdigit():
            return int(left) > int(right)
        if left.isdigit() != right.isdigit():
            return not left.isdigit()
        return left > right
    return len(candidate_pre) > len(current_pre)


def _version_sort_key(value: str) -> tuple[Any, ...]:
    major, minor, patch, prerelease = _version_parts(value)
    if prerelease is None:
        return major, minor, patch, 1, ()
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease)
    return major, minor, patch, 0, identifiers


def _safe_github_asset_url(value: Any) -> str:
    if not isinstance(value, str):
        raise UpdateCheckError('unsafe_asset_url', '发行资产下载地址无效')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise UpdateCheckError('unsafe_asset_url', '发行资产下载地址无效') from None
    if (parsed.scheme.lower() != 'https' or parsed.hostname is None or
            parsed.hostname.lower() not in {'github.com', 'objects.githubusercontent.com',
                                            'release-assets.githubusercontent.com'} or
            parsed.username is not None or parsed.password is not None or
            port not in (None, 443) or parsed.fragment):
        raise UpdateCheckError('unsafe_asset_url', '发行资产必须使用受支持的 HTTPS GitHub 地址')
    return value


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, response, code, message, headers, new_url):
        try:
            parsed = urllib.parse.urlsplit(new_url)
            port = parsed.port
        except ValueError:
            raise UpdateCheckError('unsafe_redirect', 'GitHub 请求重定向到不受支持的地址') from None
        if (parsed.scheme.lower() != 'https' or parsed.hostname is None or
                parsed.hostname.lower() not in self.allowed_hosts or parsed.username is not None or
                parsed.password is not None or port not in (None, 443) or parsed.fragment):
            raise UpdateCheckError('unsafe_redirect', 'GitHub 请求重定向到不受支持的地址')
        return super().redirect_request(request, response, code, message, headers, new_url)


def _open(request: urllib.request.Request | str, timeout: float, *, allowed_redirect_hosts: set[str]):
    """Separate the standard-library transport seam so tests can replace it."""
    opener = urllib.request.build_opener(_AllowlistRedirectHandler(allowed_redirect_hosts))
    return opener.open(request, timeout=timeout)


def _read_releases(repository: str, timeout: float) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        api_url = (f'https://api.github.com/repos/{repository}/releases'
                   f'?per_page={RELEASE_PAGE_SIZE}&page={page}')
        request = urllib.request.Request(api_url, headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'GeWuWorkbench-update-check',
        })
        try:
            with _open(request, timeout, allowed_redirect_hosts={'api.github.com'}) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateCheckError('repository_not_found', '更新仓库或 Release 不存在') from None
            raise UpdateCheckError('release_api_error', f'GitHub Release 查询失败（HTTP {exc.code}）') from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateCheckError('release_api_error', 'GitHub Release 查询失败') from exc
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateCheckError('invalid_release_response', 'Release 响应超过大小限制')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UpdateCheckError('invalid_release_response', 'Release 响应不是有效 JSON') from None
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise UpdateCheckError('invalid_release_response', 'Release 响应结构无效')
        releases.extend(data)
        if len(data) < RELEASE_PAGE_SIZE:
            return releases
    raise UpdateCheckError('release_list_too_large', 'Release 数量超过在线更新查询上限')


def _published_release(releases: list[dict[str, Any]]) -> dict[str, Any]:
    published = []
    for item in releases:
        if item.get('draft') is not False or not item.get('published_at'):
            continue
        try:
            version_key = _version_sort_key(_release_version(item))
            published_at = datetime.fromisoformat(
                str(item['published_at']).replace('Z', '+00:00')).astimezone(timezone.utc)
        except UpdateCheckError:
            continue
        except (TypeError, ValueError):
            raise UpdateCheckError('invalid_release_response', 'Release 发布时间格式无效') from None
        published.append((version_key, published_at, item))
    if not published:
        raise UpdateCheckError('no_published_release', '更新仓库没有已发布的 Release')
    return max(published, key=lambda item: (item[0], item[1]))[2]


def _release_version(release: dict[str, Any]) -> str:
    tag = release.get('tag_name')
    if not isinstance(tag, str):
        raise UpdateCheckError('invalid_release_response', 'Release 缺少版本标签')
    version = tag[1:] if tag.startswith('v') else tag
    _version_parts(version)
    return version


def _sidecar_digest(asset: dict[str, Any], timeout: float) -> str:
    url = _safe_github_asset_url(asset.get('browser_download_url'))
    try:
        with _open(url, timeout, allowed_redirect_hosts={
                'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}) as response:
            effective_url = response.geturl() if hasattr(response, 'geturl') else url
            _safe_github_asset_url(effective_url)
            raw = response.read(MAX_SIDECAR_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError('sidecar_unavailable', f'SHA-256 侧车读取失败（HTTP {exc.code}）') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError('sidecar_unavailable', 'SHA-256 侧车读取失败') from exc
    if len(raw) > MAX_SIDECAR_BYTES:
        raise UpdateCheckError('invalid_sha256', 'SHA-256 侧车超过大小限制')
    try:
        text = raw.decode('utf-8-sig').strip()
    except UnicodeDecodeError:
        raise UpdateCheckError('invalid_sha256', 'SHA-256 侧车编码无效') from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise UpdateCheckError('invalid_sha256', 'SHA-256 侧车必须只有一条记录')
    match = re.fullmatch(r'([0-9a-fA-F]{64})(?:\s+\*?([^\s]+))?', lines[0])
    if not match:
        raise UpdateCheckError('invalid_sha256', 'SHA-256 侧车格式无效')
    sidecar_name = asset['name'][:-len('.sha256')]
    if match.group(2) is not None and match.group(2) != sidecar_name:
        raise UpdateCheckError('invalid_sha256', 'SHA-256 侧车文件名与绿色包不匹配')
    return match.group(1).lower()


def _asset_digest(portable: dict[str, Any], assets: list[dict[str, Any]], timeout: float) -> str:
    digest = portable.get('digest')
    if digest is not None:
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
            raise UpdateCheckError('invalid_sha256', 'GitHub 资产 digest 不是有效 SHA-256')
        return digest.split(':', 1)[1].lower()
    sidecar_name = portable['name'] + '.sha256'
    sidecars = [item for item in assets if item.get('name') == sidecar_name]
    if len(sidecars) != 1:
        raise UpdateCheckError('missing_sha256', '绿色包缺少唯一的 SHA-256 digest 或侧车')
    return _sidecar_digest(sidecars[0], timeout)


def check_for_update(current_version: str = VERSION, *, repository: str | None = None,
                     timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Return latest published portable-release metadata without downloading the package."""
    repo = repository or os.environ.get('GEWU_UPDATE_REPOSITORY') or DEFAULT_REPOSITORY
    if not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo):
        raise UpdateCheckError('invalid_repository', '更新仓库必须是 owner/repository 格式')
    _version_parts(current_version.split('+', 1)[0])
    if timeout <= 0:
        raise UpdateCheckError('invalid_timeout', '网络超时必须大于零')

    release = _published_release(_read_releases(repo, timeout))
    latest_version = _release_version(release)
    expected_name = f'GeWuWorkbench-{latest_version}-portable.zip'
    assets = release.get('assets')
    if not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets):
        raise UpdateCheckError('invalid_release_response', 'Release 资产列表结构无效')
    portable_assets = [item for item in assets if isinstance(item.get('name'), str) and
                       item['name'].startswith('GeWuWorkbench-') and item['name'].endswith('-portable.zip')]
    if len(portable_assets) != 1 or portable_assets[0].get('name') != expected_name:
        raise UpdateCheckError('portable_asset_missing', 'Release 必须包含且只包含一个匹配版本的绿色包')
    portable = portable_assets[0]
    download_url = _safe_github_asset_url(portable.get('browser_download_url'))
    sha256 = _asset_digest(portable, assets, timeout)
    release_url = release.get('html_url')
    if not isinstance(release_url, str):
        raise UpdateCheckError('invalid_release_response', 'Release 页面地址缺失')
    try:
        parsed_release_url = urllib.parse.urlsplit(release_url)
        release_port = parsed_release_url.port
    except ValueError:
        raise UpdateCheckError('unsafe_release_url', 'Release 页面地址无效') from None
    if (parsed_release_url.scheme.lower() != 'https' or parsed_release_url.hostname is None or
            parsed_release_url.hostname.lower() != 'github.com' or parsed_release_url.username is not None or
            parsed_release_url.password is not None or release_port not in (None, 443) or parsed_release_url.fragment):
        raise UpdateCheckError('unsafe_release_url', 'Release 页面地址不是 HTTPS GitHub 地址')
    notes = release.get('body') or ''
    if not isinstance(notes, str):
        raise UpdateCheckError('invalid_release_response', 'Release 说明格式无效')
    summary = ' '.join(notes.split())[:500]
    return {
        'current_version': current_version,
        'latest_version': latest_version,
        'update_available': _is_newer(latest_version, current_version),
        'download_url': download_url,
        'sha256': sha256,
        'release_url': release_url,
        'release_notes_summary': summary,
    }
