import io
import socket

import httpx
import pytest
from PIL import Image

from backend import import_image_download as downloader


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, status=200, headers=None, chunks=(), error=None):
        self.status_code = status
        self.headers = headers or {"content-type": "image/png"}
        self.chunks = chunks
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        if self.error:
            raise self.error
        yield from self.chunks


def _mock_client(monkeypatch, response):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url):
            calls.append((method, url))
            return response

    monkeypatch.setattr(downloader.httpx, "Client", Client)
    return calls


def _public_dns(monkeypatch, addresses=("93.184.216.34",)):
    def resolve(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in addresses]

    monkeypatch.setattr(downloader.socket, "getaddrinfo", resolve)


@pytest.mark.parametrize("url", [
    "file:///tmp/image.png",
    "https://user:secret@example.com/image.png",
    "http://localhost/image.png",
    "https://api.localhost/image.png",
    "http://127.0.0.1/image.png",
    "http://10.0.0.8/image.png",
    "https://[::1]/image.png",
    "https://169.254.169.254/latest/meta-data/",
])
def test_rejects_unsafe_urls_without_network(monkeypatch, url):
    calls = _mock_client(monkeypatch, _Response(chunks=[_png()]))
    result = downloader.download_image(url)
    assert not result.ok
    assert result.error_code
    assert calls == []


def test_checks_every_dns_answer_and_rejects_mixed_public_private(monkeypatch):
    _public_dns(monkeypatch, ("93.184.216.34", "192.168.1.20"))
    calls = _mock_client(monkeypatch, _Response(chunks=[_png()]))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "blocked_address"
    assert calls == []


def test_downloads_public_image_with_bounded_streaming(monkeypatch):
    _public_dns(monkeypatch)
    payload = _png()
    calls = _mock_client(monkeypatch, _Response(
        headers={"content-type": "IMAGE/PNG; charset=binary", "content-length": str(len(payload))},
        chunks=[payload[:5], payload[5:]],
    ))
    result = downloader.download_image("https://images.example/image.png")
    assert result.ok
    assert result.data == payload
    assert result.content_type == "image/png"
    assert calls[0][1]["follow_redirects"] is False
    assert calls[0][1]["trust_env"] is False


def test_rejects_redirect_instead_of_following(monkeypatch):
    _public_dns(monkeypatch)
    calls = _mock_client(monkeypatch, _Response(status=302, headers={"location": "http://127.0.0.1/"}))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "redirect_not_allowed"
    assert len([call for call in calls if call[0] == "GET"]) == 1


@pytest.mark.parametrize("headers", [
    {"content-type": "text/html"},
    {"content-type": "image/png", "content-length": "not-a-number"},
    {"content-type": "image/png", "content-length": str(downloader.MAX_IMAGE_BYTES + 1)},
])
def test_rejects_wrong_type_or_untrusted_content_length(monkeypatch, headers):
    _public_dns(monkeypatch)
    calls = _mock_client(monkeypatch, _Response(headers=headers, chunks=[_png()]))
    result = downloader.download_image("https://images.example/image.png")
    assert not result.ok
    assert result.error_code in {"not_an_image_response", "invalid_content_length", "image_too_large"}
    assert calls


def test_hard_limit_catches_underreported_content_length(monkeypatch):
    _public_dns(monkeypatch)
    calls = _mock_client(monkeypatch, _Response(
        headers={"content-type": "image/png", "content-length": "1"},
        chunks=[b"x" * (downloader.MAX_IMAGE_BYTES // 2), b"y" * (downloader.MAX_IMAGE_BYTES // 2 + 1)],
    ))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "image_too_large"
    assert calls


def test_rejects_image_content_that_cannot_be_decoded(monkeypatch):
    _public_dns(monkeypatch)
    _mock_client(monkeypatch, _Response(chunks=[b"not really a png"]))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "invalid_image"


def test_network_timeout_is_a_reportable_failure(monkeypatch):
    _public_dns(monkeypatch)
    _mock_client(monkeypatch, _Response(error=httpx.ReadTimeout("timed out")))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "timeout"
    assert result.data is None


def test_dns_failure_is_a_reportable_failure(monkeypatch):
    def fail_resolve(*_args, **_kwargs):
        raise socket.gaierror("unavailable")

    monkeypatch.setattr(downloader.socket, "getaddrinfo", fail_resolve)
    calls = _mock_client(monkeypatch, _Response(chunks=[_png()]))
    result = downloader.download_image("https://images.example/image.png")
    assert result.error_code == "dns_error"
    assert calls == []

