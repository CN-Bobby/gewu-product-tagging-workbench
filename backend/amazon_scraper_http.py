"""Small request-body guard for Amazon scraper endpoints."""
from __future__ import annotations

from starlette.requests import Request


class AmazonRequestBodyTooLarge(Exception):
    pass


class InvalidAmazonContentLength(Exception):
    pass


async def buffer_amazon_request_body(request: Request, max_bytes: int) -> int:
    """Read at most ``max_bytes`` and cache the body for FastAPI's later JSON parser."""
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise InvalidAmazonContentLength from exc
        if declared_size < 0:
            raise InvalidAmazonContentLength
        if declared_size > max_bytes:
            raise AmazonRequestBodyTooLarge

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise AmazonRequestBodyTooLarge
        body.extend(chunk)

    # BaseHTTPMiddleware replays Request._body to the downstream FastAPI handler.
    request._body = bytes(body)
    return len(body)
