"""Bounded, public-network-only downloads for image URLs found in workbooks."""
from __future__ import annotations

import io
import ipaddress
import socket
import warnings
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 25 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ImageDownloadResult:
    """Either downloaded image bytes or a stable, user-reportable error."""

    data: bytes | None = None
    error_code: str | None = None
    error_message: str | None = None
    content_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


class _DownloadFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    # is_global excludes private, loopback, link-local, reserved, unspecified,
    # multicast and documentation ranges.
    return address.is_global and not address.is_multicast


def _validate_public_url(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url or any(ord(ch) < 32 for ch in url):
        raise _DownloadFailure("invalid_url", "图片地址格式无效")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _DownloadFailure("invalid_url", "图片地址格式无效") from exc

    if scheme not in {"http", "https"} or not host:
        raise _DownloadFailure("invalid_url", "只支持公网 HTTP 或 HTTPS 图片地址")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise _DownloadFailure("userinfo_not_allowed", "图片地址不能包含用户名或密码")

    normalized_host = host.rstrip(".").casefold()
    if (not normalized_host or normalized_host == "localhost"
            or normalized_host.endswith(".localhost")):
        raise _DownloadFailure("blocked_host", "不允许访问本机或本地域名")

    effective_port = port or (443 if scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_ip(str(literal)):
            raise _DownloadFailure("blocked_address", "不允许访问非公网 IP 地址")
        return normalized_host, effective_port

    try:
        records = socket.getaddrinfo(normalized_host, effective_port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise _DownloadFailure("dns_error", "无法解析图片地址的域名") from exc
    addresses = {record[4][0] for record in records if record and len(record) > 4}
    if not addresses:
        raise _DownloadFailure("dns_error", "图片地址的域名没有可用解析结果")
    # Reject the entire hostname if any answer is unsafe; do not pick a public
    # answer while silently ignoring a private one.
    if any(not _public_ip(address) for address in addresses):
        raise _DownloadFailure("blocked_address", "域名解析包含非公网 IP 地址")
    return normalized_host, effective_port


def download_image(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                   max_bytes: int = MAX_IMAGE_BYTES) -> ImageDownloadResult:
    """Download and decode one public image URL without following redirects.

    All failures are returned as ``ImageDownloadResult`` values so callers can
    report a per-row problem without interrupting a workbook import.
    """
    try:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise _DownloadFailure("invalid_limit", "图片大小限制配置无效")
        _validate_public_url(url)
        payload = bytearray()
        response_type = ""
        try:
            # Do not route through ambient proxies, and do not follow redirects.
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", url) as response:
                    if 300 <= response.status_code < 400:
                        raise _DownloadFailure("redirect_not_allowed", "图片地址返回了重定向，已拒绝")
                    if not 200 <= response.status_code < 300:
                        raise _DownloadFailure("http_status", f"图片地址请求失败（HTTP {response.status_code}）")

                    response_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if not response_type.startswith("image/"):
                        raise _DownloadFailure("not_an_image_response", "服务器返回的内容类型不是图片")

                    length_header = response.headers.get("content-length")
                    if length_header is not None:
                        try:
                            declared_length = int(length_header.strip())
                        except (TypeError, ValueError) as exc:
                            raise _DownloadFailure("invalid_content_length", "图片响应长度无效") from exc
                        if declared_length < 0:
                            raise _DownloadFailure("invalid_content_length", "图片响应长度无效")
                        if declared_length > max_bytes:
                            raise _DownloadFailure("image_too_large", "图片超过 25 MiB 限制")

                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        if len(payload) + len(chunk) > max_bytes:
                            raise _DownloadFailure("image_too_large", "图片超过 25 MiB 限制")
                        payload.extend(chunk)
        except _DownloadFailure:
            raise
        except httpx.TimeoutException as exc:
            raise _DownloadFailure("timeout", "下载图片超时") from exc
        except httpx.HTTPError as exc:
            raise _DownloadFailure("network_error", "下载图片时网络请求失败") from exc
        except OSError as exc:
            raise _DownloadFailure("network_error", "下载图片时网络请求失败") from exc

        if not payload:
            raise _DownloadFailure("empty_response", "图片响应内容为空")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            raise _DownloadFailure("invalid_image", "响应内容无法解码为有效图片") from exc
        return ImageDownloadResult(data=bytes(payload), content_type=response_type)
    except _DownloadFailure as exc:
        return ImageDownloadResult(error_code=exc.code, error_message=exc.message)
    except Exception:
        # Unexpected per-URL failures must not abort processing the whole workbook.
        return ImageDownloadResult(error_code="download_error", error_message="图片下载失败")

