from __future__ import annotations

import threading
import json
from urllib.error import URLError

import pytest

from backend.amazon_reviews import brightdata


ASIN = "B07NZ482JC"


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_collect_reviews_sends_review_cap_and_normalizes_snapshot(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        kwargs = {"timeout": timeout, "data": json.loads(request.data) if request.data else None,
                  "authorization": request.get_header("Authorization")}
        calls.append((request.get_method().lower(), request.full_url, kwargs))
        url = request.full_url
        if request.get_method() == "POST":
            return _Response({"snapshot_id": "s_test"})
        if "/progress/" in url:
            return _Response({"status": "ready"})
        return _Response([{
            "asin": ASIN,
            "review_title": "Works well",
            "review_text": "The item works well.",
            "review_rating": "5 out of 5 stars",
            "review_date": "2026-09-01",
            "reviewer_name": "Reviewer",
            "verified_purchase": True,
            "helpful_votes": 2,
        }])

    monkeypatch.setattr(brightdata, "urlopen", fake_urlopen)
    monkeypatch.setattr(brightdata.time, "sleep", lambda _seconds: None)

    result = brightdata.collect_reviews(ASIN, "test-api-key", cancel=threading.Event())

    assert result["reviews"][0]["asin"] == ASIN
    assert result["reviews"][0]["title"] == "Works well"
    assert result["reviews"][0]["content"] == "The item works well."
    assert result["reviews"][0]["star_rating"] == "5"
    assert result["reviews"][0]["verified_purchase"] is True
    request_body = calls[0][2]["data"]
    assert request_body[0]["url"].endswith(f"/dp/{ASIN}")
    assert request_body[0]["max_reviews"] == brightdata.MAX_REVIEWS_PER_ASIN
    assert calls[0][2]["authorization"] == "Bearer test-api-key"


def test_collect_reviews_rejects_provider_rows_for_another_asin(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.get_method() == "POST":
            return _Response({"snapshot_id": "s_test"})
        if "/progress/" in request.full_url:
            return _Response({"status": "ready"})
        return _Response([{"asin": "B000000000", "review_text": "wrong product"}])

    monkeypatch.setattr(brightdata, "urlopen", fake_urlopen)
    monkeypatch.setattr(brightdata.time, "sleep", lambda _seconds: None)

    with pytest.raises(brightdata.BrightDataError, match="没有返回可用评论"):
        brightdata.collect_reviews(ASIN, "test-api-key", cancel=threading.Event())


def test_collect_reviews_does_not_include_provider_key_in_errors(monkeypatch):
    secret = "test-api-key"

    def fail(*_args, **_kwargs):
        raise URLError(f"failed for Bearer {secret}")

    monkeypatch.setattr(brightdata, "urlopen", fail)
    with pytest.raises(brightdata.BrightDataError) as error:
        brightdata.collect_reviews(ASIN, secret, cancel=threading.Event())
    assert secret not in str(error.value)
