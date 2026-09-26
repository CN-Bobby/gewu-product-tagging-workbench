from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from backend.amazon_scraper_http import (
    AmazonRequestBodyTooLarge,
    InvalidAmazonContentLength,
    buffer_amazon_request_body,
)


def request_with(chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None):
    pending = [
        {'type': 'http.request', 'body': chunk, 'more_body': index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    calls = {'count': 0}

    async def receive():
        calls['count'] += 1
        if pending:
            return pending.pop(0)
        return {'type': 'http.disconnect'}

    scope = {
        'type': 'http', 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
        'path': '/api/amazon-scraper/preview', 'raw_path': b'/api/amazon-scraper/preview',
        'query_string': b'', 'headers': headers or [], 'server': ('testserver', 80),
        'client': ('127.0.0.1', 12345),
    }
    return Request(scope, receive), calls


def test_bounded_body_is_replayed_for_json_parser():
    request, _ = request_with([b'{"input":"B000000001"}'])
    length = asyncio.run(buffer_amazon_request_body(request, 256))
    assert length == 22
    assert asyncio.run(request.body()) == b'{"input":"B000000001"}'


def test_content_length_above_limit_is_rejected_before_reading():
    request, calls = request_with([], [(b'content-length', b'257')])
    with pytest.raises(AmazonRequestBodyTooLarge):
        asyncio.run(buffer_amazon_request_body(request, 256))
    assert calls['count'] == 0


def test_chunked_body_cannot_exceed_limit():
    request, _ = request_with([b'a' * 200, b'b' * 57])
    with pytest.raises(AmazonRequestBodyTooLarge):
        asyncio.run(buffer_amazon_request_body(request, 256))


def test_invalid_content_length_is_rejected():
    request, _ = request_with([], [(b'content-length', b'not-a-number')])
    with pytest.raises(InvalidAmazonContentLength):
        asyncio.run(buffer_amazon_request_body(request, 256))
