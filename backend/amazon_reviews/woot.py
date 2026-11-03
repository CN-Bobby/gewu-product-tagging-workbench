"""Woot-backed Amazon US review collector.

The endpoint and extraction strategy follow the MIT-licensed
amazon-review-scraper project by mrlong0129. Woot's review endpoint is an
undocumented interface and can change without notice.
"""
from __future__ import annotations

import json
import html as html_lib
import math
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

BASE_URL = "https://www.woot.com/review/Reviews/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
MAX_PAGES_PER_FILTER = 100
MAX_REVIEW_REQUEST_ATTEMPTS = 3
MAX_HTTP_REQUEST_ATTEMPTS_PER_ASIN = 500
TRANSIENT_HTTP_STATUSES = frozenset((408, 425, 429, 500, 502, 503, 504))
ASIN_PATTERN = re.compile(r"^B[A-Z0-9]{9}$", re.I)
OFFER_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
WOOT_CATALOG_HOSTS = ("sport", "sellout", "computers", "electronics", "home", "gourmet", "shirt", "tools")
WOOT_CATALOG_TTL_SECONDS = 3600
WOOT_ASIN_TTL_SECONDS = 1800
MAX_RESOLVER_CANDIDATES = 200
RESOLVER_WORKERS = 4
_CACHE_LOCK = threading.Lock()
_CATALOG_CACHE: tuple[float, list[str]] | None = None
_ASIN_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_TITLE_CACHE: dict[str, tuple[float, str]] = {}
_GENERIC_TITLE_TOKENS = {
    "a", "an", "and", "as", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
    "amazon", "essentials", "brand", "new", "official", "women", "womens", "woman", "men", "mens", "man",
}


class WootCatalogMiss(RuntimeError):
    """The complete public catalog was checked and has no exact ASIN match."""

    code = "woot_catalog_asin_not_found"


class WootResolverSearchLimit(RuntimeError):
    """The ranked candidate limit was reached without confirming an exact ASIN match."""

    code = "woot_catalog_search_limit_reached"


class WootCatalogIncomplete(RuntimeError):
    """The public catalog could not be fully inspected because some reads failed."""

    code = "woot_catalog_incomplete"


class WootCancelled(RuntimeError):
    """The review collection was cancelled before its next HTTP attempt."""

    def __init__(self, message: str, budget: "_RequestBudget | None" = None) -> None:
        self.total_request_count = budget.total if budget else 0
        self.directory_request_count = budget.directory if budget else 0
        self.review_request_count = budget.reviews if budget else 0
        super().__init__(message)


class WootRequestBudgetExceeded(RuntimeError):
    """The per-ASIN HTTP attempt budget was exhausted."""

    def __init__(self, budget: "_RequestBudget") -> None:
        self.total_request_count = budget.total
        self.directory_request_count = budget.directory
        self.review_request_count = budget.reviews
        super().__init__(
            f"Woot 单 ASIN HTTP 请求预算耗尽（{budget.total}/{budget.limit} 次；"
            f"目录解析 {budget.directory} 次，评论请求 {budget.reviews} 次）"
        )


class _RequestBudget:
    def __init__(self, limit: int, cancel: threading.Event) -> None:
        self.limit = max(0, int(limit))
        self.cancel = cancel
        self.total = 0
        self.directory = 0
        self.reviews = 0
        self.lock = threading.Lock()

    def before_attempt(self, category: str) -> None:
        with self.lock:
            if self.cancel.is_set():
                raise WootCancelled("Woot 评论采集已取消", self)
            if self.total >= self.limit:
                raise WootRequestBudgetExceeded(self)
            self.total += 1
            if category == "review":
                self.reviews += 1
            else:
                self.directory += 1


def _fetch_text(url: str, *, timeout: float = 15, budget: _RequestBudget | None = None,
                cancel: threading.Event | None = None) -> str:
    """Fetch a public product/catalog page with bounded transient retries."""
    request = Request(url, headers={
        **HEADERS,
        "Accept": "text/html,application/xml,application/xhtml+xml,*/*;q=0.8",
    })
    last_error: Exception | None = None
    for attempt in range(MAX_REVIEW_REQUEST_ATTEMPTS):
        if cancel is not None and cancel.is_set():
            raise WootCancelled("Woot 评论采集已取消", budget)
        if budget is not None:
            budget.before_attempt("directory")
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_HTTP_STATUSES:
                raise
        except (OSError, TimeoutError) as exc:
            last_error = exc
        if attempt < MAX_REVIEW_REQUEST_ATTEMPTS - 1:
            time.sleep(0.25 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Woot 页面请求失败")


def _title_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _amazon_product_title(asin: str, *, budget: _RequestBudget | None = None,
                          cancel: threading.Event | None = None) -> str:
    asin = str(asin or "").strip().upper()
    with _CACHE_LOCK:
        cached = _TITLE_CACHE.get(asin)
        if cached and cached[0] > time.time():
            return cached[1]
    try:
        page = _fetch_text(f"https://www.amazon.com/dp/{asin}", timeout=20, budget=budget, cancel=cancel)
    except (WootCancelled, WootRequestBudgetExceeded):
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法从 Amazon 美国站读取商品标题（{type(exc).__name__}）") from None
    match = re.search(r'<span\b[^>]*\bid=["\']productTitle["\'][^>]*>(.*?)</span>', page, re.I | re.S)
    title = _title_text(match.group(1)) if match else ""
    if not title:
        match = re.search(r'<meta\b[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)', page, re.I)
        title = _title_text(match.group(1)) if match else ""
    if not title or title.lower() in {"amazon.com", "amazon.com: online shopping"}:
        raise RuntimeError("Amazon 美国站未返回可用于匹配的商品标题，请稍后重试")
    with _CACHE_LOCK:
        _TITLE_CACHE[asin] = (time.time() + WOOT_ASIN_TTL_SECONDS, title)
    return title


def _sitemap_offer_urls(host: str, *, budget: _RequestBudget | None = None,
                        cancel: threading.Event | None = None) -> list[str]:
    sitemap_url = f"https://{host}.woot.com/sitemap.xml"
    page = _fetch_text(sitemap_url, timeout=20, budget=budget, cancel=cancel)
    root = ET.fromstring(page)
    result: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "loc" or not node.text:
            continue
        candidate = node.text.strip()
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if (parsed.scheme == "https" and parsed.hostname == f"{host}.woot.com"
                and parsed.path.startswith("/offers/") and not parsed.username and not parsed.password):
            result.append(candidate)
    return result


def _catalog_offer_urls(*, budget: _RequestBudget | None = None,
                        cancel: threading.Event | None = None) -> tuple[list[str], list[str]]:
    """Read and cache public Woot offer URLs; report hosts that could not be scanned."""
    global _CATALOG_CACHE
    with _CACHE_LOCK:
        if _CATALOG_CACHE and _CATALOG_CACHE[0] > time.time():
            return list(_CATALOG_CACHE[1]), []
    urls: set[str] = set()
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=RESOLVER_WORKERS) as pool:
        futures = {pool.submit(_sitemap_offer_urls, host, budget=budget, cancel=cancel): host
                   for host in WOOT_CATALOG_HOSTS}
        for future in as_completed(futures):
            host = futures[future]
            try:
                urls.update(future.result())
            except (WootCancelled, WootRequestBudgetExceeded):
                for pending in futures:
                    pending.cancel()
                raise
            except Exception:  # noqa: BLE001
                failures.append(host)
    result = sorted(urls)
    if not result and len(failures) == len(WOOT_CATALOG_HOSTS):
        raise WootCatalogIncomplete("无法读取 Woot 公开商品目录，请稍后重试")
    if not failures:
        with _CACHE_LOCK:
            _CATALOG_CACHE = (time.time() + WOOT_CATALOG_TTL_SECONDS, result)
    return result, sorted(failures)


def _slug_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", unquote(str(value or ""))).encode("ascii", "ignore").decode("ascii")
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", normalized.lower()):
        if len(token) < 2 or token in _GENERIC_TITLE_TOKENS:
            continue
        # Normalize common English plurals so a title's "pants" can match a
        # Woot slug's "pant" without conflating short words.
        if len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        tokens.add(token)
    return tokens


def _rank_offer_urls(title: str, urls: list[str]) -> list[str]:
    title_tokens = _slug_tokens(title)
    if not title_tokens:
        return []
    token_sets = [(url, _slug_tokens(urlparse(url).path.rsplit("/", 1)[-1])) for url in urls]
    document_frequency: dict[str, int] = {}
    for _, tokens in token_sets:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    scored = []
    for url, tokens in token_sets:
        overlap = title_tokens & tokens
        if not overlap:
            continue
        score = sum(math.log((len(token_sets) + 1) / (document_frequency.get(token, 0) + 1)) + 1
                    for token in overlap)
        scored.append((-score, -len(overlap), url))
    scored.sort()
    return [url for _, _, url in scored]


def _offer_ids_from_html(page: str, asin: str) -> dict[str, str] | None:
    asin = str(asin or "").strip().upper()
    match = re.search(r"\bvar\s+offerItems\s*=\s*(\[.*?\])\s*;", page, re.I | re.S)
    if not match:
        return None
    try:
        items = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    exact_items = [item for item in items if isinstance(item, dict)
                   and str(item.get("Asin") or item.get("ASIN") or item.get("asin") or "").strip().upper() == asin]
    if not exact_items:
        return None
    offer_match = re.search(r'\bvar\s+offerId\s*=\s*(["\'])([^"\']+)\1', page, re.I)
    offerid = offer_match.group(2).strip() if offer_match else ""
    itemid = str(exact_items[0].get("Id") or exact_items[0].get("ItemId") or "").strip()
    if not OFFER_ID_PATTERN.fullmatch(offerid) or not itemid or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", itemid):
        return None
    return {"itemid": itemid, "offerid": offerid}


def _fetch_offer_ids(url: str, asin: str, *, budget: _RequestBudget | None = None,
                     cancel: threading.Event | None = None) -> dict[str, str] | None:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() not in
            {f"{host}.woot.com" for host in WOOT_CATALOG_HOSTS} or not parsed.path.startswith("/offers/")):
        return None
    try:
        return _offer_ids_from_html(_fetch_text(url, timeout=15, budget=budget, cancel=cancel), asin)
    except HTTPError as exc:
        if exc.code == 404:
            return None  # stale URL in a public sitemap
        raise


def _resolve_offer_ids(asin: str, *, budget: _RequestBudget | None = None,
                       cancel: threading.Event | None = None) -> dict[str, str]:
    """Resolve an Amazon ASIN to a currently listed Woot offer, verifying exact ASIN."""
    asin = str(asin or "").strip().upper()
    if not ASIN_PATTERN.fullmatch(asin):
        raise ValueError("ASIN 格式无效")
    with _CACHE_LOCK:
        cached = _ASIN_CACHE.get(asin)
        if cached and cached[0] > time.time():
            return dict(cached[1])

    title = _amazon_product_title(asin, budget=budget, cancel=cancel)
    urls, sitemap_failures = _catalog_offer_urls(budget=budget, cancel=cancel)
    ranked_candidates = _rank_offer_urls(title, urls)
    search_limit_reached = len(ranked_candidates) > MAX_RESOLVER_CANDIDATES
    candidates = ranked_candidates[:MAX_RESOLVER_CANDIDATES]
    if not candidates:
        if sitemap_failures:
            raise WootCatalogIncomplete("Woot 公开商品目录读取不完整，请稍后重试或粘贴 Woot 评论链接")
        raise WootCatalogMiss("Woot 当前公开商品目录没有找到与该 ASIN 精确匹配的商品")

    match: dict[str, str] | None = None
    page_failures = 0
    with ThreadPoolExecutor(max_workers=RESOLVER_WORKERS) as pool:
        futures = {pool.submit(_fetch_offer_ids, url, asin, budget=budget, cancel=cancel): url
                   for url in candidates}
        try:
            for future in as_completed(futures):
                try:
                    match = future.result()
                except (WootCancelled, WootRequestBudgetExceeded):
                    raise
                except Exception:  # noqa: BLE001
                    page_failures += 1
                if match:
                    break
        finally:
            for future in futures:
                future.cancel()
    if match:
        with _CACHE_LOCK:
            _ASIN_CACHE[asin] = (time.time() + WOOT_ASIN_TTL_SECONDS, dict(match))
        return match
    if sitemap_failures or page_failures:
        raise WootCatalogIncomplete("Woot 公开商品目录检索不完整，请稍后重试或粘贴 Woot 评论链接")
    if search_limit_reached:
        raise WootResolverSearchLimit(
            f"Woot 已检查前 {MAX_RESOLVER_CANDIDATES} 个候选页，仍未确认精确 ASIN 匹配；搜索结果不完整"
        )
    raise WootCatalogMiss("Woot 当前公开商品目录没有找到与该 ASIN 精确匹配的商品")


def parse_review_url(asin: str, value: str) -> dict[str, str]:
    """Validate a public Woot review-page link and extract its request IDs."""
    asin = str(asin or "").strip().upper()
    if not ASIN_PATTERN.fullmatch(asin):
        raise ValueError("ASIN 格式无效")
    try:
        parsed = urlparse(str(value or "").strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("Woot 评论链接无效") from None
    if (parsed.scheme != "https" or not (hostname == "woot.com" or hostname.endswith(".woot.com"))
            or parsed.username or parsed.password or port):
        raise ValueError("Woot 评论链接必须来自 HTTPS 的 Woot 网站")
    match = re.fullmatch(r"/review/(?:Reviews/)?(B[A-Z0-9]{9})/?", parsed.path, re.I)
    if not match or match.group(1).upper() != asin:
        raise ValueError("Woot 评论链接中的 ASIN 与任务 ASIN 不一致")
    params = parse_qs(parsed.query, keep_blank_values=True)
    item_ids = params.get("itemid", [])
    offer_ids = params.get("offerid", [])
    itemid = item_ids[0].strip() if len(item_ids) == 1 else ""
    offerid = offer_ids[0].strip() if len(offer_ids) == 1 else ""
    if not itemid or len(itemid) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", itemid):
        raise ValueError("Woot 评论链接缺少有效的 itemid")
    if not OFFER_ID_PATTERN.fullmatch(offerid):
        raise ValueError("Woot 评论链接缺少有效的 offerid")
    return {"itemid": itemid, "offerid": offerid}


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


def _fetch_filter(asin: str, star: int, sort: int, *, itemid: str | None = None,
                  offerid: str | None = None, budget: _RequestBudget | None = None,
                  cancel: threading.Event | None = None) -> tuple[list[dict[str, Any]], int]:
    if bool(itemid) != bool(offerid):
        raise ValueError("Woot 评论请求必须同时提供 itemid 和 offerid")
    rows: list[dict[str, Any]] = []
    paging_next = ""
    request_count = 0
    for page in range(1, MAX_PAGES_PER_FILTER + 1):
        params = {"filter": str(star), "isVerified": "false", "sort": str(sort)}
        if itemid and offerid:
            # The current Woot review AJAX endpoint accepts offerid only. The
            # itemid remains useful for validating pasted review links, but
            # including it in this request shape causes Woot to return 404.
            params["offerid"] = offerid
        params.update({"pagingNext": paging_next} if paging_next else {"page": "1"})
        url = BASE_URL + asin + "?" + urlencode(params)
        referer = f"https://www.woot.com/review/{asin}"
        if itemid and offerid:
            referer += "?" + urlencode({"offerid": offerid})
        request = Request(url, headers={**HEADERS, "Referer": referer})
        for attempt in range(MAX_REVIEW_REQUEST_ATTEMPTS):
            if cancel is not None and cancel.is_set():
                raise WootCancelled("Woot 评论采集已取消", budget)
            if budget is not None:
                budget.before_attempt("review")
            request_count += 1
            try:
                with urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code == 404:
                    raise RuntimeError(
                        "Woot 评论接口返回 404。该商品可能没有可访问的评论，或 Woot 接口已变化；请切换 Sorftime MCP。"
                    ) from None
                if exc.code in TRANSIENT_HTTP_STATUSES and attempt < MAX_REVIEW_REQUEST_ATTEMPTS - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"Woot 评论请求失败（星级 {star}、排序 {sort}、页 {page}）：HTTP {exc.code}，"
                    f"已尝试 {attempt + 1} 次"
                ) from None
            except (OSError, TimeoutError) as exc:
                if attempt < MAX_REVIEW_REQUEST_ATTEMPTS - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"Woot 评论请求失败（星级 {star}、排序 {sort}、页 {page}）：{type(exc).__name__}，"
                    f"已尝试 {attempt + 1} 次"
                ) from None
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Woot 评论请求失败（星级 {star}、排序 {sort}、页 {page}）：{type(exc).__name__}"
                ) from None
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


def collect_reviews(asin: str, progress: Callable[[str], None] | None = None, *,
                    itemid: str | None = None, offerid: str | None = None,
                    cancel: threading.Event | None = None) -> dict[str, Any]:
    """Collect and deduplicate Woot's five-star-by-four-sort review slices."""
    from ..external_effects import assert_allowed
    assert_allowed("amazon_collection")
    asin = str(asin or "").strip().upper()
    if not ASIN_PATTERN.fullmatch(asin):
        raise ValueError("ASIN 格式无效")
    if bool(itemid) != bool(offerid):
        raise ValueError("Woot 评论请求必须同时提供 itemid 和 offerid")
    cancel = cancel or threading.Event()
    budget = _RequestBudget(MAX_HTTP_REQUEST_ATTEMPTS_PER_ASIN, cancel)
    if cancel.is_set():
        raise WootCancelled("Woot 评论采集已取消", budget)
    if not offerid:
        if progress:
            progress("Woot 正在通过 Amazon 商品标题查找评论页面")
        resolved = _resolve_offer_ids(asin, budget=budget, cancel=cancel)
        itemid = resolved["itemid"]
        offerid = resolved["offerid"]
    seen: set[tuple[str, str, str]] = set()
    reviews: list[dict[str, Any]] = []
    request_count = 0
    for combination, (star, sort) in enumerate(
            ((star, sort) for star in (5, 4, 3, 2, 1) for sort in (0, 1, 2, 3)), start=1):
        if progress:
            progress(f"Woot 组合 {combination}/20（{star} 星，排序 {sort}）")
        if cancel.is_set():
            raise WootCancelled("Woot 评论采集已取消", budget)
        batch, count = _fetch_filter(asin, star, sort, itemid=itemid, offerid=offerid,
                                     budget=budget, cancel=cancel)
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
    return {"reviews": reviews, "request_count": budget.reviews, "total_request_count": budget.total,
            "directory_request_count": budget.directory, "review_request_count": budget.reviews,
            "mode": "max"}
