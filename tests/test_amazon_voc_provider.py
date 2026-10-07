from __future__ import annotations

from pathlib import Path

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
