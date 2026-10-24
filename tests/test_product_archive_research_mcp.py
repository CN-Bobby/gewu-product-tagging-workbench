import hashlib
import json
from pathlib import Path

import httpx
import pytest

from backend.product_archive import research_mcp


PUBLIC_DNS = lambda _host, _port, type=None: [
    (None, None, None, None, ("93.184.216.34", 443))
]


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_tools(tmp_path, responses, monkeypatch, **kwargs):
    from backend import external_effects

    monkeypatch.setattr(external_effects, "assert_allowed", lambda _service: None)
    client = FakeClient(responses)
    resolver = kwargs.pop("resolver", PUBLIC_DNS)
    tools = research_mcp.ResearchTools(
        ledger_path=tmp_path / "research.jsonl",
        key_provider=lambda: "synthetic-test-secret",
        client_factory=lambda: client,
        resolver=resolver,
        **kwargs,
    )
    return tools, client


def read_ledger(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_search_returns_stable_evidence_ids_and_writes_one_bounded_row_per_result(tmp_path, monkeypatch):
    content = "x" * 2000
    url = "https://example.com/category"
    tools, client = make_tools(tmp_path, [FakeResponse(body={"results": [{
        "title": "Useful page", "url": url, "snippet": content, "rerank_score": 2.2,
    }]})], monkeypatch)

    result = tools.search("market signals", count=3)

    expected_id = "web:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    assert "evidence_id" not in result["results"][0]
    assert result["citation_status"] == "candidate_only"
    assert result["results"][0]["excerpt"] == content[:research_mcp.MAX_LEDGER_EXCERPT_CHARS]
    assert client.calls[0][0] == research_mcp.SEARCH_ENDPOINT
    assert client.calls[0][1]["json"] == {"query": "market signals", "count": 3}
    assert client.calls[0][1]["timeout"] == research_mcp.REQUEST_TIMEOUT_SECONDS
    assert client.calls[0][1]["headers"]["Authorization"] == "Bearer synthetic-test-secret"
    [row] = read_ledger(tmp_path / "research.jsonl")
    assert row == {
        "kind": "search", "query": "market signals", "evidence_id": expected_id,
        "url": url, "title": "Useful page", "excerpt": content[:1500], "fetched": False,
        "status": "success",
    }
    assert "content" not in row


def test_fetch_returns_bounded_content_and_a_ledger_row_without_full_content_or_key(tmp_path, monkeypatch):
    key = "synthetic-test-secret"
    body = "Page text " * 1000
    url = "https://example.com/guide?q=summer"
    tools, client = make_tools(tmp_path, [FakeResponse(body={"title": "Guide", "content": body})], monkeypatch)
    tools.key_provider = lambda: key

    result = tools.fetch(url)

    expected_id = "web:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    assert result["evidence_id"] == expected_id
    assert result["content"] == body[:research_mcp.MAX_EXCERPT_CHARS]
    assert result["content_truncated"] is True
    [row] = read_ledger(tmp_path / "research.jsonl")
    assert row["kind"] == "fetch" and row["fetched"] is True and row["status"] == "success"
    assert row["url"] == url and row["evidence_id"] == expected_id and row["title"] == "Guide"
    assert len(row["excerpt"]) <= 1500
    assert body not in json.dumps(row)
    assert key not in json.dumps(row)
    assert client.calls[0][1]["follow_redirects"] is False


def test_separate_hard_budgets_are_reserved_before_requests_and_never_exceeded(tmp_path, monkeypatch):
    tools, client = make_tools(tmp_path, [
        FakeResponse(body={"results": []}),
        FakeResponse(body={"title": "Page", "content": "short"}),
    ], monkeypatch, search_budget=1, fetch_budget=1)
    tools.search("one")
    tools.fetch("https://example.com/page")

    with pytest.raises(research_mcp.ResearchMCPError, match="次数已达上限"):
        tools.search("two")
    with pytest.raises(research_mcp.ResearchMCPError, match="次数已达上限"):
        tools.fetch("https://example.com/other")
    assert len(client.calls) == 2


@pytest.mark.parametrize("url", [
    "http://example.com/", "https://localhost/", "https://x.local/", "https://127.0.0.1/",
    "https://[::1]/", "https://user:password@example.com/", "https://example.com:8443/",
    "https://example.com/?api_key=secret", "https://example.com/?%61pi_key=secret",
])
def test_fetch_rejects_non_https_private_or_credential_urls_without_network_calls(tmp_path, monkeypatch, url):
    tools, client = make_tools(tmp_path, [], monkeypatch)
    with pytest.raises(research_mcp.ResearchMCPError):
        tools.fetch(url)
    assert client.calls == []


def test_fetch_rejects_a_public_hostname_that_resolves_to_private_address(tmp_path, monkeypatch):
    private_dns = lambda *_args, **_kwargs: [(None, None, None, None, ("10.0.0.8", 443))]
    tools, client = make_tools(tmp_path, [], monkeypatch, resolver=private_dns)
    with pytest.raises(research_mcp.ResearchMCPError, match="公网地址"):
        tools.fetch("https://example.com/")
    assert client.calls == []


def test_deepseek_redirect_is_refused_without_following_or_retry(tmp_path, monkeypatch):
    tools, client = make_tools(tmp_path, [FakeResponse(status_code=302, headers={"location": "https://evil.invalid"})], monkeypatch)
    with pytest.raises(research_mcp.ResearchMCPError, match="重定向"):
        tools.search("one")
    assert len(client.calls) == 1
    [row] = read_ledger(tmp_path / "research.jsonl")
    assert row["status"] == "http_302"
    assert row["fetched"] is False


def test_timeout_is_not_retried_and_error_or_ledger_never_contains_provider_key(tmp_path, monkeypatch):
    key = "synthetic-test-secret"
    tools, client = make_tools(tmp_path, [httpx.ReadTimeout(key)], monkeypatch)
    tools.key_provider = lambda: key
    with pytest.raises(research_mcp.ResearchMCPError, match="不会自动重试") as error:
        tools.search("public product category")
    assert key not in str(error.value)
    assert len(client.calls) == 1
    [row] = read_ledger(tmp_path / "research.jsonl")
    assert row["status"] == "timeout"
    assert key not in json.dumps(row)


def test_offline_gate_rejects_before_budget_or_http_request(tmp_path, monkeypatch):
    from backend import external_effects

    def denied(service):
        raise external_effects.ExternalEffectsDisabled(f"offline {service}")

    monkeypatch.setattr(external_effects, "assert_allowed", denied)
    client = FakeClient([])
    tools = research_mcp.ResearchTools(
        ledger_path=tmp_path / "research.jsonl", key_provider=lambda: "key",
        client_factory=lambda: client, resolver=PUBLIC_DNS,
    )
    with pytest.raises(external_effects.ExternalEffectsDisabled):
        tools.search("query")
    assert tools._counts["search"] == 0
    assert client.calls == []
    assert not (tmp_path / "research.jsonl").exists()


def test_vault_resolver_uses_configured_deepseek_credential_without_returning_it_in_errors(tmp_path, monkeypatch):
    from backend import model_settings, runtime_isolation, vision

    config_path = tmp_path / "config.toml"
    config_path.write_text("[provider]\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_SEARCH_API_KEY", "unselected-environment-secret")
    monkeypatch.setattr(runtime_isolation, "provider_home", lambda: tmp_path)
    monkeypatch.setattr(model_settings, "_canonical_config", lambda _config: ({
        "providers": [{"name": "deepseek", "default": "deepseek-flash", "models": ["deepseek-flash"]}]
    }, False))
    monkeypatch.setattr(runtime_isolation, "credential_selection", lambda model_ref, credential_id=None: ("deepseek", "cred-a"))
    monkeypatch.setattr(vision, "provider_config", lambda model_ref, credential_id: ("hidden", "vault-secret", "deepseek-flash"))
    assert research_mcp.resolve_deepseek_key() == "vault-secret"


def test_runner_ledger_path_is_used_as_the_per_run_jsonl_destination(tmp_path, monkeypatch):
    ledger = tmp_path / "archive-run" / "web-ledger.jsonl"
    monkeypatch.setenv("ARCHIVE_RESEARCH_LEDGER", str(ledger))
    monkeypatch.setenv("ARCHIVE_RESEARCH_RUN_ID", "run-123")
    assert research_mcp._default_ledger_path() == ledger
