"""Connection retry behavior at the keyword research LLM boundary."""
import time

import httpx
import pytest

from backend.keyword_research import llm_client as llm_client_module
from backend.keyword_research.llm_client import LLMClient, LLMError


def _responses_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "mock-model",
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "{}"}],
            }],
        },
        request=httpx.Request("POST", "https://model.example/responses"),
    )


def test_connect_refusal_retries_with_bounded_backoff_then_succeeds(monkeypatch):
    client = LLMClient("https://model.example", "secret", "mock-model")
    calls = []
    delays = []
    failure = httpx.ConnectError(
        "connection refused", request=httpx.Request("POST", "https://model.example/responses")
    )

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise failure
        return _responses_ok()

    monkeypatch.setattr(llm_client_module.httpx, "post", post)
    monkeypatch.setattr(time, "sleep", delays.append)

    result = client.complete([{"role": "user", "content": "hello"}])

    assert result.text == "{}"
    assert result.mode == "responses"
    assert calls == ["https://model.example/responses"] * 3
    assert delays == [0.5, 1.0]


def test_connect_refusal_exhaustion_raises_without_protocol_fallback(monkeypatch):
    client = LLMClient("https://model.example", "secret", "mock-model")
    calls = []
    delays = []

    def post(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError(
            "connection refused", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(llm_client_module.httpx, "post", post)
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(LLMError) as raised:
        client.complete([{"role": "user", "content": "hello"}])

    assert raised.value.retryable is True
    assert calls == ["https://model.example/responses"] * 4
    assert delays == [0.5, 1.0, 2.0]


def test_read_timeout_is_not_retried_or_fallen_back_to_chat(monkeypatch):
    client = LLMClient("https://model.example", "secret", "mock-model")
    calls = []
    delays = []

    def post(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("response timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client_module.httpx, "post", post)
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(LLMError) as raised:
        client.complete([{"role": "user", "content": "hello"}])

    assert raised.value.retryable is False
    assert calls == ["https://model.example/responses"]
    assert delays == []


def test_auto_mode_keeps_nonretryable_responses_fallback(monkeypatch):
    client = LLMClient("https://model.example", "secret", "mock-model")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("/responses"):
            return httpx.Response(
                404, text="unsupported", request=httpx.Request("POST", url)
            )
        return httpx.Response(
            200,
            json={"model": "mock-model", "choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm_client_module.httpx, "post", post)

    result = client.complete([{"role": "user", "content": "hello"}])

    assert result.mode == "chat"
    assert result.text == "{}"
    assert calls == [
        "https://model.example/responses",
        "https://model.example/chat/completions",
    ]


def test_malformed_success_response_is_not_retried(monkeypatch):
    client = LLMClient("https://model.example", "secret", "mock-model", mode="responses")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200, content=b"{", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(llm_client_module.httpx, "post", post)

    with pytest.raises(ValueError):
        client.complete([{"role": "user", "content": "hello"}])

    assert calls == ["https://model.example/responses"]
