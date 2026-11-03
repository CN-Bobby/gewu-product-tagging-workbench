"""Capped Amazon US review collection through Bright Data's Reviews dataset API."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.brightdata.com/datasets/v3"
REVIEWS_DATASET_ID = "gd_le8e811kzy4ggddlq"
MAX_REVIEWS_PER_ASIN = 20
POLL_INTERVAL_SECONDS = 2.0
MAX_WAIT_SECONDS = 240.0
HTTP_TIMEOUT_SECONDS = 60
ASIN_PATTERN = re.compile(r"^B[A-Z0-9]{9}$", re.I)
SNAPSHOT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


class BrightDataError(RuntimeError):
    """Safe, user-facing provider failure without a response body or API key."""


class BrightDataCancelled(BrightDataError):
    """Collection was cancelled while waiting for a provider snapshot."""


def _request_json(method: str, url: str, api_key: str, action: str,
                  payload: Any = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except HTTPError as exc:
        if exc.code == 401:
            raise BrightDataError("Bright Data Key 无效或无权调用评论采集服务") from None
        raise BrightDataError(f"Bright Data {action}失败（HTTP {exc.code}）") from None
    except (URLError, TimeoutError, OSError):
        raise BrightDataError(f"Bright Data {action}失败") from None
    if status >= 400:
        if status == 401:
            raise BrightDataError("Bright Data Key 无效或无权调用评论采集服务")
        raise BrightDataError(f"Bright Data {action}失败（HTTP {status}）")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BrightDataError(f"Bright Data {action}返回格式无法识别") from None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for name in ("reviews", "items", "data", "records", "results"):
        value = payload.get(name)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "verified", "1"}:
        return True
    if text in {"false", "no", "unverified", "0"}:
        return False
    return None


def _asin_from_row(row: dict[str, Any]) -> str:
    value = str(row.get("asin") or row.get("ASIN") or row.get("product_id") or "").strip().upper()
    if ASIN_PATTERN.fullmatch(value):
        return value
    url = str(row.get("url") or row.get("product_url") or "")
    match = re.search(r"/dp/([A-Z0-9]{10})(?:[/?#]|$)", url, re.I)
    return match.group(1).upper() if match else ""


def _normalize_review(row: dict[str, Any], requested_asin: str) -> dict[str, Any] | None:
    row_asin = _asin_from_row(row)
    if row_asin and row_asin != requested_asin:
        return None
    title = str(row.get("review_title") or row.get("title") or row.get("review_headline") or "").strip()
    content = str(row.get("review_text") or row.get("review_content") or row.get("content")
                  or row.get("review") or row.get("text") or row.get("body") or "").strip()
    if not title and not content:
        return None
    rating_value = row.get("review_rating") or row.get("rating") or row.get("star_rating") or row.get("stars")
    rating_match = re.search(r"(?<!\d)([1-5](?:\.\d+)?)(?!\d)", str(rating_value or ""))
    rating = rating_match.group(1) if rating_match else ""
    return {
        "asin": requested_asin,
        "review_id": str(row.get("review_id") or row.get("id") or ""),
        "review_url": str(row.get("review_url") or ""),
        "title": title,
        "content": content,
        "star_rating": rating,
        "review_date": str(row.get("review_date") or row.get("date") or row.get("review_date_utc") or ""),
        "author": str(row.get("reviewer_name") or row.get("author_name") or row.get("author") or ""),
        "verified_purchase": _bool(row.get("verified_purchase", row.get("is_verified_purchase"))),
        "helpful_votes": row.get("helpful_votes") or row.get("helpful_count") or 0,
        "variant_attribute": str(row.get("variant") or row.get("variation") or ""),
    }


def _extract_reviews(payload: Any, asin: str) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for row in _rows(payload):
        normalized = _normalize_review(row, asin)
        if not normalized:
            continue
        identity = (normalized["review_id"] or normalized["review_url"]
                    or f"{normalized['review_date']}|{normalized['title']}|{normalized['content']}")
        if identity in seen:
            continue
        seen.add(identity)
        result.append(normalized)
        if len(result) >= MAX_REVIEWS_PER_ASIN:
            break
    return result


def collect_reviews(asin: str, api_key: str, *, cancel: threading.Event | None = None,
                    progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Collect at most 20 provider records for one validated Amazon US ASIN."""
    asin = str(asin or "").strip().upper()
    if not ASIN_PATTERN.fullmatch(asin):
        raise BrightDataError("ASIN 格式无效")
    if not isinstance(api_key, str) or not api_key.strip():
        raise BrightDataError("请先在总设置中配置 Bright Data API Key")
    cancel = cancel or threading.Event()
    input_row = {"url": f"https://www.amazon.com/dp/{asin}", "reviews_to_not_include": [],
                 "max_reviews": MAX_REVIEWS_PER_ASIN, "variation_specific": True}
    try:
        if cancel.is_set():
            raise BrightDataCancelled("Bright Data 评论采集已取消")
        trigger_url = f"{API_ROOT}/trigger?" + urlencode({
            "dataset_id": REVIEWS_DATASET_ID, "format": "json",
            "uncompressed_webhook": "true", "include_errors": "true", "notify": "false",
        })
        payload = _request_json("POST", trigger_url, api_key.strip(), "任务提交", [input_row])
        if not isinstance(payload, dict) or not payload.get("snapshot_id"):
            reviews = _extract_reviews(payload, asin)
            if not reviews:
                raise BrightDataError("Bright Data 没有返回可用评论")
            return {"reviews": reviews, "request_count": 1, "snapshot_id": ""}
        snapshot_id = str(payload["snapshot_id"])
        if not SNAPSHOT_PATTERN.fullmatch(snapshot_id):
            raise BrightDataError("Bright Data 返回的任务编号格式无效")
        started = time.monotonic()
        request_count = 1
        while True:
            if cancel.is_set():
                raise BrightDataCancelled("Bright Data 评论采集已取消")
            if time.monotonic() - started > MAX_WAIT_SECONDS:
                raise BrightDataError("Bright Data 评论任务等待超时；可重试该 ASIN")
            if progress:
                progress("Bright Data 正在处理评论请求")
            status = _request_json("GET", f"{API_ROOT}/progress/{quote(snapshot_id, safe='')}",
                                   api_key.strip(), "任务状态查询")
            request_count += 1
            if not isinstance(status, dict):
                raise BrightDataError("Bright Data 返回的任务状态无法识别")
            state = str(status.get("status") or "").lower()
            if state == "ready":
                break
            if state in {"failed", "canceled", "cancelled"}:
                raise BrightDataError("Bright Data 评论采集任务失败或被取消")
            if state not in {"starting", "running"}:
                raise BrightDataError("Bright Data 返回未知任务状态")
            cancel.wait(POLL_INTERVAL_SECONDS)
        if cancel.is_set():
            raise BrightDataCancelled("Bright Data 评论采集已取消")
        result_payload = _request_json("GET", f"{API_ROOT}/snapshot/{quote(snapshot_id, safe='')}?format=json",
                                        api_key.strip(), "结果下载")
        request_count += 1
        reviews = _extract_reviews(result_payload, asin)
        if not reviews:
            raise BrightDataError("Bright Data 没有返回可用评论")
        return {"reviews": reviews, "request_count": request_count, "snapshot_id": snapshot_id}
    except BrightDataError:
        raise
    except (URLError, TimeoutError, OSError):
        raise BrightDataError("Bright Data 网络请求失败；请稍后重试") from None
