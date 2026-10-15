from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from backend.amazon_voc.runtime.voc_app.provider import ModelError, OpenAICompatibleProvider


def _response(value):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps(value).encode("utf-8")
    return Response()


def _settings():
    return SimpleNamespace(model_api_key="local-test-key", model_base_url="https://model.invalid/v1",
                           model_protocol="chat")


def _completion(content):
    return {"choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2}}


def test_deepseek_chat_uses_json_object_mode_without_schema_probe(monkeypatch):
    requests = []
    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return _response(_completion({"ok": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result, _usage = OpenAICompatibleProvider(_settings()).call(
        "test_task", "deepseek/deepseek-v4-flash", "Return structured content", {"sample": 1},
        {"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert result == {"ok": True}
    assert len(requests) == 1
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "合法 JSON 对象" in requests[0]["messages"][0]["content"]


def test_schema_compatibility_fallback_does_not_persist_remote_error_body(monkeypatch):
    requests = []
    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 400, "bad request", {},
                io.BytesIO(b'{"message":"response_format unavailable; PRIVATE_REVIEW_TEXT"}'))
        return _response(_completion({"ok": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result, _usage = OpenAICompatibleProvider(_settings()).call(
        "test_task", "generic-chat-model", "Return structured content", {"sample": 1},
        {"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert result == {"ok": True}
    assert len(requests) == 2
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["response_format"] == {"type": "json_object"}


def test_non_schema_400_is_not_retried_and_error_body_is_not_exposed(monkeypatch):
    calls = []
    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 400, "bad request", {},
            io.BytesIO(b'{"message":"payload too large; PRIVATE_REVIEW_TEXT"}'))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ModelError) as raised:
        OpenAICompatibleProvider(_settings()).call(
            "test_task", "generic-chat-model", "Return structured content", {"sample": 1},
            {"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert len(calls) == 1
    assert raised.value.retryable is False
    assert raised.value.status_code == 400
    assert "PRIVATE_REVIEW_TEXT" not in str(raised.value)


@pytest.mark.parametrize("status,retryable", [(401, False), (402, False), (429, True), (500, True)])
def test_http_error_retry_classification_and_redaction(monkeypatch, status, retryable):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "error", {},
            io.BytesIO(b'{"message":"PRIVATE_REVIEW_TEXT"}'))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ModelError) as raised:
        OpenAICompatibleProvider(_settings()).call(
            "test_task", "generic-chat-model", "Return structured content", {"sample": 1},
            {"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert "PRIVATE_REVIEW_TEXT" not in str(raised.value)
