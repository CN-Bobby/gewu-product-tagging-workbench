"""Perform one explicitly requested, one-result SellerSprite probe for the swimwear archive.

The script uses the workbench credential store selected by TAG_STORAGE, reads one ASIN from the
existing archive input.json, initializes MCP and lists tools, then makes at most one business call.
It writes a key-free one-time marker before network access and does not copy the source workbook.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_error(value: Any, secret: str) -> str:
    text = str(value or "request failed")
    if secret:
        text = text.replace(secret, "[hidden]")
    text = re.sub(r"(?i)([?&](?:key|api[_-]?key|secret-key)=)[^&\s]+", r"\1[hidden]", text)
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [hidden]", text)
    text = re.sub(r"https?://[^\s\"'<>]+", "[service address hidden]", text)
    return text.replace("\r", " ").replace("\n", " ")[:300]


def _load_first_us_asin(run_dir: Path) -> str:
    source = run_dir / "input.json"
    if not source.is_file():
        raise ValueError("指定的泳衣档案缺少 input.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    for group in data.get("groups", []):
        if str(group.get("marketplace") or "").upper() != "US":
            continue
        for value in group.get("asins") or []:
            candidate = str(value).strip().upper()
            if len(candidate) == 10 and candidate.startswith("B") and candidate.isalnum():
                return candidate
    raise ValueError("档案中没有可用于美国站验证的 ASIN")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required to make the one real SellerSprite query")
    parser.add_argument("--storage", required=True, help="workbench Data-Test directory")
    parser.add_argument("--archive-run", required=True, help="existing swimwear research run directory")
    args = parser.parse_args()
    if not args.live:
        parser.error("live network access requires --live")

    storage = Path(args.storage).expanduser().resolve()
    run_dir = Path(args.archive_run).expanduser().resolve()
    if not storage.is_dir() or not run_dir.is_dir():
        parser.error("storage or selected archive directory does not exist")
    if not run_dir.is_relative_to(storage / "keyword-research"):
        parser.error("selected archive must be under this storage's keyword-research directory")
    os.environ["TAG_STORAGE"] = str(storage)
    repository = Path(__file__).resolve().parents[1]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))

    from backend.keyword_research import credentials
    from backend.keyword_research.call_log import CallLog
    from backend.keyword_research.mcp_client import MCPClient, MCPError, test_mcp_connection
    from backend.keyword_research.rate_limiter import RateLimiter

    secret = credentials.read_secret()
    if not secret:
        print(json.dumps({"status": "credential_missing", "business_calls": 0}, ensure_ascii=False))
        return 2
    asin = _load_first_us_asin(run_dir)
    marker = storage / "keyword-research" / "phase1-real-query-attempt.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"started_at": datetime.now(timezone.utc).isoformat(), "status": "in_progress",
                       "marketplace": "US", "business_calls_allowed": 1}, stream, ensure_ascii=False)
            stream.write("\n")
    except FileExistsError:
        print(json.dumps({"status": "one_time_probe_already_attempted", "business_calls": 0}, ensure_ascii=False))
        return 3

    result: dict[str, Any] = {"status": "connection_failed", "tools": 0, "business_calls": 0}
    headers = credentials.mcp_headers()
    endpoint = credentials.mcp_endpoint()
    try:
        handshake = test_mcp_connection(endpoint, timeout=45,
                                        rate_limiter=RateLimiter(max_calls=10, window_seconds=60, min_interval=0),
                                        headers=headers)
        if not handshake.get("ok"):
            message = str(handshake.get("error") or "")
            low = message.casefold()
            result["status"] = "authentication_rejected" if any(token in low for token in ("401", "403", "unauthorized", "forbidden")) else "connection_failed"
            result["error"] = _safe_error(message, secret)
        else:
            result["tools"] = int(handshake.get("tools") or 0)
            with tempfile.TemporaryDirectory(prefix="gewu-ss-phase1-") as scratch:
                client = MCPClient(endpoint, RateLimiter(max_calls=10, window_seconds=60, min_interval=0),
                                   CallLog(Path(scratch) / "calls.json"), max_temp_retries=0,
                                   request_timeout=45, headers=headers)
                result["business_calls"] = 1
                try:
                    data = asyncio.run(client.traffic_extend("US", [asin], page=1, size=1))
                    payload = data.get("data") if isinstance(data, dict) else None
                    payload = payload if isinstance(payload, dict) else {}
                    items = payload.get("items") or []
                    total = payload.get("total")
                    if total is None:
                        total = len(items) if isinstance(items, list) else 0
                    result.update({"status": "success_with_data" if items or int(total or 0) > 0 else "success_no_data",
                                   "returned_items": len(items) if isinstance(items, list) else 0,
                                   "reported_total": int(total or 0),
                                   "service_code": str(data.get("code") or "")[:40]})
                except MCPError as exc:
                    message = _safe_error(exc, secret)
                    result.update({"status": "authentication_rejected" if exc.unauthorized else "business_rejected",
                                   "error": message})
    except Exception as exc:  # The one-time marker prevents an automatic repeat after any failure.
        result.update({"status": "connection_failed", "error": _safe_error(exc, secret)})
    finally:
        secret = ""
        headers = {}
        marker.write_text(json.dumps({"finished_at": datetime.now(timezone.utc).isoformat(),
                                      "status": result["status"], "business_calls": result["business_calls"],
                                      "marketplace": "US"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"success_with_data", "success_no_data"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
