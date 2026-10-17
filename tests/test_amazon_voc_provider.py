from __future__ import annotations

import io
import json
from pathlib import Path
import urllib.error
from types import SimpleNamespace

import pytest

from backend.amazon_voc.runtime.voc_app.config import Settings
from backend.amazon_voc.runtime.voc_app.provider import ModelError, OpenAICompatibleProvider


def _settings(protocol: str = "responses") -> Settings:
    return Settings(
        data_dir=Path("."), database_path=Path("voc.sqlite3"),
        model_base_url="https://api.deepseek.com", model_api_key="test-only",
        model_protocol=protocol, extractor_model="deepseek-v4-flash",
        reviewer_model="deepseek-v4-flash",
    )


def _chat_response(value):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps(value).encode("utf-8")
    return Response()


def _chat_settings():
    return SimpleNamespace(model_api_key="local-test-key", model_base_url="https://model.invalid/v1",
                           model_protocol="chat")


def _chat_completion(content):
    return {"choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2}}


def test_deepseek_chat_uses_json_object_mode_without_schema_probe(monkeypatch):
    requests = []
    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return _chat_response(_chat_completion({"ok": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result, _usage = OpenAICompatibleProvider(_chat_settings()).call(
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
        return _chat_response(_chat_completion({"ok": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result, _usage = OpenAICompatibleProvider(_chat_settings()).call(
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
        OpenAICompatibleProvider(_chat_settings()).call(
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
        OpenAICompatibleProvider(_chat_settings()).call(
            "test_task", "generic-chat-model", "Return structured content", {"sample": 1},
            {"type": "object", "properties": {"ok": {"type": "boolean"}}})

    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert "PRIVATE_REVIEW_TEXT" not in str(raised.value)


def test_responses_incomplete_status_is_rejected_with_usage_preserved(monkeypatch):
    provider = OpenAICompatibleProvider(_settings())
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": '{"overview":{}}',
        "usage": {"input_tokens": 120, "output_tokens": 64000,
                  "input_tokens_details": {"cached_tokens": 80}},
    })

    with pytest.raises(ModelError, match="status=incomplete, reason=max_output_tokens") as exc:
        provider.call("full_voc_v2_synthesis", "deepseek-v4-flash", "instructions", {}, {})

    assert exc.value.retryable is False
    assert exc.value.usage["input_tokens"] == 120
    assert exc.value.usage["output_tokens"] == 64000
    assert exc.value.usage["cache_hit_input_tokens"] == 80


def test_responses_completed_status_returns_parsed_json(monkeypatch):
    provider = OpenAICompatibleProvider(_settings())
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: {
        "status": "completed", "output_text": '{"ok":true}',
        "usage": {"input_tokens": 12, "output_tokens": 4,
                  "input_tokens_details": {"cached_tokens": 3}},
    })

    result, usage = provider.call("full_voc_v2_synthesis", "deepseek-v4-flash", "instructions", {}, {})

    assert result == {"ok": True}
    assert usage["input_tokens"] == 12
    assert usage["cache_hit_input_tokens"] == 3


@pytest.mark.parametrize("protocol,limit_key", [("responses", "max_output_tokens"), ("chat", "max_tokens")])
def test_v2_assignment_uses_roomier_output_budget(monkeypatch, protocol, limit_key):
    provider = OpenAICompatibleProvider(_settings(protocol))
    captured = {}

    def fake_request(_url, body, timeout=None):
        captured.update(body)
        if protocol == "responses":
            return {"status": "completed", "output_text": '{"event_assignments":[]}',
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        return {"choices": [{"message": {"content": '{"event_assignments":[]}'} }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(provider, "_request", fake_request)
    provider.call("full_voc_v2_assign", "deepseek-v4-flash", "instructions", {}, {})

    assert captured[limit_key] == 16_384
    if protocol == "responses":
        assert captured["reasoning"] == {"effort": "none"}
    else:
        assert captured["thinking"] == {"type": "disabled"}
