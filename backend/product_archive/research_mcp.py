"""Narrow, budgeted DeepSeek Search/Fetch MCP tools for Product Archive research.

The API key is resolved from the current DEV provider vault on first use. Request and result bodies are never persisted
to the per-run ledger.
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import socket
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx


SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
REQUEST_TIMEOUT_SECONDS = 25.0
MAX_SEARCH_CALLS = 6
MAX_FETCH_CALLS = 8
MAX_SEARCH_RESULTS = 10
MAX_EXCERPT_CHARS = 6000
MAX_TITLE_CHARS = 240
MAX_LEDGER_EXCERPT_CHARS = 1500
MAX_URL_CHARS = 8192
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\b\s*[:=]\s*)[^\s,;&]+"
)


class ResearchMCPError(ValueError):
    """Safe, user-facing tool error that never embeds provider response text."""


class ResearchTools:
    """One Product Archive run's bounded Search/Fetch session."""

    def __init__(self, *, ledger_path: str | Path, key_provider=None, client_factory=None,
                 search_budget: int = MAX_SEARCH_CALLS, fetch_budget: int = MAX_FETCH_CALLS,
                 timeout: float = REQUEST_TIMEOUT_SECONDS, resolver=None):
        if not isinstance(search_budget, int) or isinstance(search_budget, bool) or not 0 <= search_budget <= MAX_SEARCH_CALLS:
            raise ValueError("search_budget must be between 0 and the hard maximum")
        if not isinstance(fetch_budget, int) or isinstance(fetch_budget, bool) or not 0 <= fetch_budget <= MAX_FETCH_CALLS:
            raise ValueError("fetch_budget must be between 0 and the hard maximum")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > REQUEST_TIMEOUT_SECONDS:
            raise ValueError("timeout must be positive and no greater than the hard maximum")
        self.ledger_path = Path(ledger_path)
        self.key_provider = key_provider or resolve_deepseek_key
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=False))
        self.search_budget = search_budget
        self.fetch_budget = fetch_budget
        self.timeout = float(timeout)
        self.resolver = resolver or socket.getaddrinfo
        self._counts = {"search": 0, "fetch": 0}
        self._queries_by_evidence_id: dict[str, str] = {}
        self._lock = threading.RLock()

    def search(self, query: str, count: int = MAX_SEARCH_RESULTS) -> dict:
        from ..external_effects import assert_allowed

        assert_allowed("external_research")
        query = _validate_query(query)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_SEARCH_RESULTS:
            raise ResearchMCPError(f"count must be between 1 and {MAX_SEARCH_RESULTS}")
        self._reserve("search")
        result = self._request(
            kind="search", endpoint=SEARCH_ENDPOINT, request_body={"query": query, "count": count},
            ledger_subject={"query": _safe_ledger_text(query)},
        )
        # Search snippets are candidates, not read evidence. Only fetch exposes
        # an evidence ID to the model, so it cannot cite an unread search hit.
        return {"results": [{key: value for key, value in row.items() if key != "evidence_id"}
                            for row in result["results"]],
                "result_count": result["result_count"], "citation_status": "candidate_only"}

    def fetch(self, url: str) -> dict:
        from ..external_effects import assert_allowed

        assert_allowed("external_research")
        validated_url = validate_public_https_url(url, resolver=self.resolver)
        self._reserve("fetch")
        result = self._request(
            kind="fetch", endpoint=FETCH_ENDPOINT, request_body={"url": validated_url},
            ledger_subject={"url": _ledger_url(validated_url)},
        )
        return result

    def _reserve(self, kind: str) -> None:
        with self._lock:
            budget = self.search_budget if kind == "search" else self.fetch_budget
            if self._counts[kind] >= budget:
                raise ResearchMCPError(f"本轮{kind}调用次数已达上限（{budget}）")
            # Reserve before key lookup/network dispatch; errors consume one slot and cannot create
            # a retry loop that exceeds the run's hard request ceiling.
            self._counts[kind] += 1

    def _request(self, *, kind: str, endpoint: str, request_body: dict,
                 ledger_subject: dict) -> dict:
        status = "failed"
        result = None
        key = ""
        try:
            candidate_key = self.key_provider()
            if not isinstance(candidate_key, str) or not candidate_key:
                raise ResearchMCPError("DeepSeek Search/Fetch 授权未配置")
            key = candidate_key
            if any(key in str(value) for value in request_body.values() if isinstance(value, str)):
                raise ResearchMCPError("请求内容包含授权凭据，已拒绝")
            with self.client_factory() as client:
                response = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=request_body,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            response_code = int(response.status_code)
            if 300 <= response_code < 400:
                status = f"http_{response_code}"
                raise ResearchMCPError("DeepSeek Search/Fetch 服务返回了重定向，已拒绝")
            if response_code >= 400:
                status = f"http_{response_code}"
                raise ResearchMCPError(f"DeepSeek Search/Fetch 请求失败（HTTP {response_code}）")
            try:
                data = response.json()
            except Exception as error:
                raise ResearchMCPError("DeepSeek Search/Fetch 返回了无法解析的响应") from None
            if not isinstance(data, dict):
                raise ResearchMCPError("DeepSeek Search/Fetch 返回格式无效")
            if kind == "search":
                result = self._search_result(data, key, self.resolver)
                for row in result["results"]:
                    self._queries_by_evidence_id[row["evidence_id"]] = ledger_subject["query"]
            else:
                result = self._fetch_result(data, key, request_body["url"])
            status = "success"
            return result
        except httpx.TimeoutException as error:
            status = "timeout"
            raise ResearchMCPError("DeepSeek Search/Fetch 请求超时；本工具不会自动重试") from None
        except httpx.HTTPError as error:
            status = "network_error"
            raise ResearchMCPError("DeepSeek Search/Fetch 网络请求失败；本工具不会自动重试") from None
        finally:
            if status == "success" and kind == "search" and result is not None:
                rows = result["results"]
                if rows:
                    for row in rows:
                        self._append_ledger({"kind": "search", "query": ledger_subject["query"],
                                             "evidence_id": row["evidence_id"], "url": row["url"],
                                             "title": row["title"], "excerpt": row["excerpt"][:MAX_LEDGER_EXCERPT_CHARS],
                                             "fetched": False, "status": status}, secret=key)
                else:
                    self._append_ledger({"kind": "search", "query": ledger_subject["query"],
                                         "evidence_id": None, "url": "", "title": "", "excerpt": "",
                                         "fetched": False, "status": "empty", "result_count": 0}, secret=key)
            elif status == "success" and kind == "fetch" and result is not None:
                self._append_ledger({"kind": "fetch", **ledger_subject, "status": status,
                                     "evidence_id": result["evidence_id"], "title": result["title"],
                                     "excerpt": result["content"][:MAX_LEDGER_EXCERPT_CHARS], "fetched": True,
                                     "query": self._queries_by_evidence_id.get(result["evidence_id"], ""),
                                     "publisher": urlsplit(ledger_subject["url"]).hostname or "",
                                     "retrieved_at": datetime.now(timezone.utc).isoformat()}, secret=key)
            else:
                self._append_ledger({"kind": kind, **ledger_subject, "status": status,
                                     "evidence_id": None, "url": ledger_subject.get("url", ""),
                                     "title": "", "excerpt": "", "fetched": False}, secret=key)

    @staticmethod
    def _search_result(data: dict, key: str, resolver) -> dict:
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ResearchMCPError("DeepSeek Search 返回格式无效：缺少结果列表")
        results = []
        for row in rows[:MAX_SEARCH_RESULTS]:
            if not isinstance(row, dict):
                continue
            raw_url = row.get("url")
            if not isinstance(raw_url, str):
                continue
            # Do not even expose a search hit whose target is local, private, malformed or carries
            # credential-like query parameters. Fetch repeats this check at dispatch time.
            try:
                result_url = validate_public_https_url(raw_url, resolver=resolver)
            except ResearchMCPError:
                continue
            results.append({
                "title": _clean_text(row.get("title"), MAX_TITLE_CHARS, key),
                "url": result_url,
                "evidence_id": evidence_id(result_url),
                "excerpt": _clean_text(row.get("snippet") or row.get("summary"), MAX_LEDGER_EXCERPT_CHARS, key),
                "rerank_score": _safe_score(row.get("rerank_score")),
            })
        return {"results": results, "result_count": len(results)}

    @staticmethod
    def _fetch_result(data: dict, key: str, url: str) -> dict:
        title = data.get("title", "")
        content = data.get("content", "")
        if not isinstance(title, str) or not isinstance(content, str):
            raise ResearchMCPError("DeepSeek Fetch 返回格式无效")
        if not content.strip():
            raise ResearchMCPError("DeepSeek Fetch 没有返回可用正文")
        return {
            "evidence_id": evidence_id(url),
            "title": _clean_text(title, MAX_TITLE_CHARS, key),
            "content": _clean_text(content, MAX_EXCERPT_CHARS, key),
            "content_truncated": len(content) > MAX_EXCERPT_CHARS,
        }

    def _append_ledger(self, record: dict, secret: str = "") -> None:
        safe = {key: _safe_ledger_text(value, secret) if isinstance(value, str) else value
                for key, value in record.items()}
        line = json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line)
        except OSError as error:
            # A failed ledger write must fail closed: the paid request has already occurred, so the
            # caller must know the evidence trail could not be retained.
            raise ResearchMCPError("研究调用账本写入失败") from error


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ResearchMCPError("query 必须为 1 到 128 个字符的非空文本")
    return query.strip()


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def validate_public_https_url(url: str, *, resolver=None) -> str:
    """Validate an HTTPS destination and require DNS resolution to public addresses only."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ResearchMCPError(f"URL 必须为 1 到 {MAX_URL_CHARS} 个字符")
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ResearchMCPError("URL 格式无效") from None
    if parsed.scheme.casefold() != "https" or not host or parsed.username is not None or parsed.password is not None:
        raise ResearchMCPError("仅允许不含用户名或密码的 HTTPS 公网链接")
    if port not in (None, 443):
        raise ResearchMCPError("仅允许 HTTPS 默认端口")
    if "%" in host:
        raise ResearchMCPError("URL 主机格式无效")
    host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ResearchMCPError("不允许本机或内部网络地址")
    resolve = resolver or socket.getaddrinfo
    try:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = list(dict.fromkeys(
                row[4][0].split("%", 1)[0] for row in resolve(host, 443, type=socket.SOCK_STREAM)
                if row and len(row) >= 5 and row[4]
            ))
    except (OSError, ValueError) as error:
        raise ResearchMCPError("URL 主机无法解析为公网地址") from None
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise ResearchMCPError("URL 必须解析到公网地址，已拒绝本机或内网目标")
    query_keys = {name.casefold() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(re.search(r"key|token|secret|password|auth", name) for name in query_keys):
        raise ResearchMCPError("URL 查询参数疑似包含凭据，已拒绝")
    path = parsed.path or "/"
    # Fragments are never sent to the provider. The same canonical URL is used for evidence identity
    # and its ledger row; credential-like query parameters are refused above.
    return urlunsplit(("https", host, path, parsed.query, ""))


def _ledger_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))[:MAX_URL_CHARS]


def _safe_score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if -100 <= value <= 100 else None


def _clean_text(value, limit: int, secret: str) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace(secret, "[REDACTED]") if secret else value
    text = _SECRET_PATTERN.sub(r"\1[REDACTED]", text)
    return text[:limit]


def _safe_ledger_text(value: str, secret: str = "") -> str:
    text = str(value).replace(secret, "[REDACTED]") if secret else str(value)
    text = _SECRET_PATTERN.sub(r"\1[REDACTED]", text)
    return text[:MAX_URL_CHARS]


def evidence_id(url: str) -> str:
    return "web:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def resolve_deepseek_key() -> str:
    """Read DeepSeek's selected credential from the configured provider vault."""
    from .. import model_settings
    from ..runtime_isolation import credential_selection, provider_home
    from ..vision import provider_config

    provider_name = "deepseek"
    config_path = Path(provider_home()) / "config.toml"
    if not config_path.is_file():
        raise ResearchMCPError("DeepSeek Search/Fetch 授权未配置")
    try:
        config, _ = model_settings._canonical_config(tomllib.loads(config_path.read_text(encoding="utf-8-sig")))
    except Exception as error:
        raise ResearchMCPError("DeepSeek Search/Fetch 授权配置不可用") from None
    provider = next((item for item in config.get("providers", []) if item.get("name") == provider_name), None)
    if not provider:
        raise ResearchMCPError("DeepSeek Search/Fetch 授权未配置")
    model = provider.get("default") or provider.get("model") or next(iter(provider.get("models", [])), None)
    if not isinstance(model, str) or not model:
        raise ResearchMCPError("DeepSeek Search/Fetch 模型配置不可用")
    model_ref = f"{provider_name}/{model}"
    frozen_id = (os.environ.get("ARCHIVE_RESEARCH_DEEPSEEK_CREDENTIAL_ID") or
                 os.environ.get("TAG_DEEPSEEK_CREDENTIAL_ID"))
    try:
        _, credential_id = credential_selection(model_ref, frozen_id)
        _, key, _ = provider_config(model_ref, credential_id)
    except Exception as error:
        raise ResearchMCPError("DeepSeek Search/Fetch 授权不可用") from None
    return key


def _default_ledger_path() -> Path:
    configured = (os.environ.get("ARCHIVE_RESEARCH_LEDGER") or "").strip()
    if configured:
        return Path(configured)
    run_id = (os.environ.get("ARCHIVE_RESEARCH_RUN_ID") or
              os.environ.get("TAG_PRODUCT_ARCHIVE_RESEARCH_RUN_ID") or "").strip()
    storage = (os.environ.get("TAG_STORAGE") or "").strip()
    if not _RUN_ID_PATTERN.fullmatch(run_id) or not storage:
        raise ResearchMCPError("缺少本轮 Product Archive 研究账本配置")
    return Path(storage) / "product_archive" / "research" / f"{run_id}.jsonl"


_DEFAULT_TOOLS: ResearchTools | None = None
_DEFAULT_LOCK = threading.Lock()


def _tools() -> ResearchTools:
    global _DEFAULT_TOOLS
    with _DEFAULT_LOCK:
        if _DEFAULT_TOOLS is None:
            _DEFAULT_TOOLS = ResearchTools(ledger_path=_default_ledger_path())
        return _DEFAULT_TOOLS


def create_mcp_server():
    """Build the stdio MCP server lazily, keeping pure unit tests independent of MCP imports."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("product_archive_research")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def search(query: str, count: int = MAX_SEARCH_RESULTS) -> dict:
        """Search public web pages through DeepSeek Search; bounded by the current research run."""
        return _tools().search(query, count)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def fetch(url: str) -> dict:
        """Fetch one validated public HTTPS page through DeepSeek Fetch; returns a bounded excerpt."""
        return _tools().fetch(url)

    return server


def main() -> None:
    """Run the narrow research server over MCP stdio."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
