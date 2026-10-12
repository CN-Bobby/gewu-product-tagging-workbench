"""Woot-backed Amazon US review collector.

The endpoint and extraction strategy follow the MIT-licensed
amazon-review-scraper project by mrlong0129. Woot's review endpoint is an
undocumented interface and can change without notice.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://www.woot.com/review/Reviews/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
MAX_PAGES_PER_FILTER = 100


def _review_date(value: Any) -> str:
    description = str(value or "")
    patterns = (
        r"on\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, description)
        if not match:
            continue
        normalized = re.sub(r"\s+", " ", match.group(1).replace(",", " ")).strip()
        for date_format in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(normalized, date_format).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


def _normalize_review(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    rating = row.get("OverallRating") or row.get("overallRating") or row.get("rating") or ""
    return {
        **row,
        "author": row.get("Author") or row.get("author") or "",
        "title": row.get("Title") or row.get("title") or "",
        "content": row.get("Text") or row.get("text") or row.get("content") or "",
        "star_rating": rating,
        "rating": rating,
        # SubmissionDate has been observed as a bogus epoch value; use the
        # human-readable date from OriginDescription instead.
        "review_date": _review_date(row.get("OriginDescription")) or row.get("review_date") or "",
        "is_verified_purchase": row.get("IsVerifiedPurchase", row.get("is_verified_purchase", False)),
        "is_vine_review": row.get("IsVineReview", row.get("is_vine_review", False)),
        "helpful_votes": row.get("HelpfulVotes", row.get("helpful_votes", 0)),
    }


def _fetch_filter(asin: str, star: int, sort: int) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    paging_next = ""
    request_count = 0
    for page in range(1, MAX_PAGES_PER_FILTER + 1):
        params = {"filter": str(star), "isVerified": "false", "sort": str(sort)}
        params.update({"pagingNext": paging_next} if paging_next else {"page": "1"})
        url = BASE_URL + asin + "?" + urlencode(params)
        request = Request(url, headers={**HEADERS, "Referer": f"https://www.woot.com/review/{asin}"})
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Woot 评论请求失败（星级 {star}、排序 {sort}、页 {page}）：{type(exc).__name__}") from None
        request_count += 1
        if not isinstance(payload, dict):
            raise ValueError("Woot 返回的数据格式无效")
        batch = payload.get("Reviews")
        if not isinstance(batch, list):
            raise ValueError("Woot 返回结果中没有评论列表")
        rows.extend(review for value in batch if (review := _normalize_review(value)) is not None)
        paging_next = str(payload.get("PagingNext") or "")
        if not batch or not paging_next:
            break
        if page == MAX_PAGES_PER_FILTER:
            raise RuntimeError("Woot 评论分页超过安全上限，请稍后重试")
        time.sleep(0.15)
    return rows, request_count


def collect_reviews(asin: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Collect and deduplicate Woot's five-star-by-four-sort review slices."""
    from ..external_effects import assert_allowed
    assert_allowed("amazon_collection")
    seen: set[tuple[str, str, str]] = set()
    reviews: list[dict[str, Any]] = []
    request_count = 0
    for combination, (star, sort) in enumerate(
            ((star, sort) for star in (5, 4, 3, 2, 1) for sort in (0, 1, 2, 3)), start=1):
        if progress:
            progress(f"Woot 组合 {combination}/20（{star} 星，排序 {sort}）")
        batch, count = _fetch_filter(asin, star, sort)
        request_count += count
        for review in batch:
            identity = (
                str(review.get("author") or ""),
                str(review.get("title") or ""),
                str(review.get("content") or "")[:80],
            )
            if identity not in seen:
                seen.add(identity)
                reviews.append(review)
    return {"reviews": reviews, "request_count": request_count, "mode": "max"}
