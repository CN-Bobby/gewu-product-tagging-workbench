import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import pytest

from backend.amazon_reviews import service, woot


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


def test_collect_reviews_queries_twenty_filters_and_deduplicates(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return Response({'Reviews': [{
            'Author': 'Reviewer', 'Title': 'Good', 'Text': 'Works well',
            'OverallRating': 5, 'OriginDescription': 'Reviewed on June 25, 2025',
        }], 'PagingNext': ''})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)
    monkeypatch.setattr(woot.time, 'sleep', lambda delay: sleeps.append(delay))
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    monkeypatch.setattr(woot, '_resolve_offer_ids', lambda _asin, **_kwargs: {
        'itemid': 'resolved-item', 'offerid': '00000000-0000-0000-0000-000000000001',
    })
    progress = []

    result = woot.collect_reviews('B012345678', progress.append)

    assert len(calls) == 20
    assert all(timeout == 15 for _, timeout in calls)
    assert len(result['reviews']) == 1
    assert result['request_count'] == 20
    assert result['total_request_count'] == 20
    assert result['directory_request_count'] == 0
    assert result['review_request_count'] == 20
    assert result['reviews'][0]['review_date'] == '2025-06-25'
    assert result['reviews'][0]['star_rating'] == 5
    assert len(progress) == 21
    assert progress[0].startswith('Woot 正在通过 Amazon 商品标题')
    assert progress[1].startswith('Woot 组合 1/20')
    assert progress[-1].startswith('Woot 组合 20/20')
    assert sleeps == []


def test_collect_reviews_stops_at_shared_asin_request_budget(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    monkeypatch.setattr(woot, 'MAX_HTTP_REQUEST_ATTEMPTS_PER_ASIN', 2, raising=False)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if 'woot.com/review/' in request.full_url:
            return Response({'Reviews': [], 'PagingNext': 'next-page'})
        return Response({'unused': True})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)

    with pytest.raises(RuntimeError, match='请求预算耗尽') as exc_info:
        woot.collect_reviews(
            'B012345678', itemid='resolved-item',
            offerid='00000000-0000-0000-0000-000000000001',
        )

    assert len(calls) == 2
    assert exc_info.value.total_request_count == 2
    assert exc_info.value.directory_request_count == 0
    assert exc_info.value.review_request_count == 2


def test_collect_reviews_counts_each_retry_against_asin_budget(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    monkeypatch.setattr(woot, 'MAX_HTTP_REQUEST_ATTEMPTS_PER_ASIN', 2, raising=False)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 502, 'Bad Gateway', None, None)
        return Response({'Reviews': [], 'PagingNext': ''})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)
    monkeypatch.setattr(woot.time, 'sleep', lambda _delay: None)

    with pytest.raises(RuntimeError, match='请求预算耗尽') as exc_info:
        woot.collect_reviews(
            'B012345678', itemid='resolved-item',
            offerid='00000000-0000-0000-0000-000000000001',
        )

    assert len(calls) == 2  # first request, retry, then the next filter is refused
    assert exc_info.value.total_request_count == 2
    assert exc_info.value.review_request_count == 2


def test_resolver_and_review_requests_share_budget_and_report_breakdown(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    monkeypatch.setattr(woot, 'MAX_HTTP_REQUEST_ATTEMPTS_PER_ASIN', 21, raising=False)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        if 'amazon.com/dp/' in request.full_url:
            return Response({'unused': True})
        return Response({'Reviews': [], 'PagingNext': ''})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)
    def resolve(asin, *, budget, cancel):
        woot._fetch_text(f'https://catalog.woot.com/sitemap.xml', budget=budget, cancel=cancel)
        return {'itemid': 'resolved-item', 'offerid': '00000000-0000-0000-0000-000000000001'}

    monkeypatch.setattr(woot, '_resolve_offer_ids', resolve)

    result = woot.collect_reviews('B012345678')

    assert len(requests) == 21
    assert 'catalog.woot.com/sitemap.xml' in requests[0]
    assert all('woot.com/review/' in url for url in requests[1:])
    assert result['total_request_count'] == 21
    assert result['directory_request_count'] == 1
    assert result['review_request_count'] == 20
    assert result['request_count'] == result['review_request_count'] == 20
    assert result['total_request_count'] == (
        result['directory_request_count'] + result['review_request_count']
    )


def test_collect_reviews_checks_cancellation_before_first_http_attempt(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    cancel = __import__('threading').Event()
    cancel.set()
    requests = []
    monkeypatch.setattr(woot, 'urlopen', lambda request, timeout: requests.append(request.full_url))

    with pytest.raises(RuntimeError, match='已取消'):
        woot.collect_reviews('B012345678', cancel=cancel)

    assert requests == []


def test_collect_reviews_does_not_retry_after_cancellation(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    cancel = __import__('threading').Event()
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        cancel.set()
        raise HTTPError(request.full_url, 502, 'Bad Gateway', None, None)

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)

    with pytest.raises(RuntimeError, match='已取消'):
        woot.collect_reviews(
            'B012345678', cancel=cancel, itemid='resolved-item',
            offerid='00000000-0000-0000-0000-000000000001',
        )

    assert len(requests) == 1


def test_fetch_filter_follows_paging_next_and_obeys_page_limit(monkeypatch):
    calls = []
    payloads = [
        {'Reviews': [{'Author': 'A', 'Title': 'First', 'Text': 'One'}], 'PagingNext': 'next-token'},
        {'Reviews': [{'Author': 'B', 'Title': 'Second', 'Text': 'Two'}], 'PagingNext': ''},
    ]

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return Response(payloads.pop(0))

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)
    monkeypatch.setattr(woot.time, 'sleep', lambda _delay: None)
    reviews, request_count = woot._fetch_filter('B012345678', 5, 0)

    assert len(reviews) == 2
    assert request_count == 2
    assert 'pagingNext=next-token' in calls[1]


def test_fetch_filter_retries_transient_http_error_and_counts_each_attempt(monkeypatch):
    requested = []
    sleeps = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        if len(requested) == 1:
            raise HTTPError(request.full_url, 502, "Bad Gateway", None, None)
        return Response({'Reviews': [{'Author': 'Reviewer', 'Title': 'Useful', 'Text': 'Works'}],
                         'PagingNext': ''})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)
    monkeypatch.setattr(woot.time, 'sleep', sleeps.append)

    reviews, request_count = woot._fetch_filter(
        'B012345678', 5, 0, itemid='WTTEST123A',
        offerid='00000000-0000-0000-0000-000000000001',
    )

    assert len(requested) == 2
    assert request_count == 2
    assert sleeps == [0.25]
    assert reviews[0]['content'] == 'Works'


def test_parse_review_url_extracts_ids_only_for_matching_woot_asin():
    ids = woot.parse_review_url(
        'B012345678',
        'https://electronics.woot.com/review/B012345678?itemid=WTTEST123A&offerid=00000000-0000-0000-0000-000000000001',
    )

    assert ids == {'itemid': 'WTTEST123A', 'offerid': '00000000-0000-0000-0000-000000000001'}


def test_parse_review_url_rejects_non_woot_hosts_mismatched_asin_and_missing_ids():
    invalid_urls = [
        'https://woot.com.attacker.test/review/B012345678?itemid=WTTEST123A&offerid=00000000-0000-0000-0000-000000000001',
        'https://electronics.woot.com/review/B098765432?itemid=WTTEST123A&offerid=00000000-0000-0000-0000-000000000001',
        'https://electronics.woot.com/review/B012345678?itemid=WTTEST123A',
        'http://electronics.woot.com/review/B012345678?itemid=WTTEST123A&offerid=00000000-0000-0000-0000-000000000001',
    ]

    for url in invalid_urls:
        try:
            woot.parse_review_url('B012345678', url)
        except ValueError:
            pass
        else:
            raise AssertionError(f'无效 Woot 评论链接不应被接受：{url}')


def test_service_binds_woot_review_links_to_exact_requested_asins():
    url = 'https://electronics.woot.com/review/B012345678?itemid=WTTEST123A&offerid=00000000-0000-0000-0000-000000000001'
    resolved = service._woot_review_ids({'B012345678': url}, ['B012345678'], 'woot')

    assert resolved == {'B012345678': {
        'itemid': 'WTTEST123A', 'offerid': '00000000-0000-0000-0000-000000000001',
    }}

    try:
        service._woot_review_ids({'B012345678': url}, ['B098765432'], 'woot')
    except ValueError as exc:
        assert '任务范围' in str(exc)
    else:
        raise AssertionError('不能把其他 ASIN 的 Woot 链接绑定到任务商品')


def test_create_rejects_ranked_woot_before_any_external_configuration(monkeypatch):
    monkeypatch.setattr(service.sorftime_settings, 'connection', lambda: (_ for _ in ()).throw(
        AssertionError('榜单模式选择 Woot 时应在读取 Sorftime 配置前拦截')
    ))

    with pytest.raises(ValueError, match='榜单模式请切换到 Sorftime MCP'):
        service.create({
            'mode': 'ranked', 'site': 'US', 'node_id': '12345',
            'collection_source': 'woot', 'translate_enabled': False,
        })


def test_fetch_filter_includes_resolved_woot_ids(monkeypatch):
    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        return Response({'Reviews': [], 'PagingNext': ''})

    monkeypatch.setattr(woot, 'urlopen', fake_urlopen)

    woot._fetch_filter(
        'B012345678', 5, 0, itemid='WTTEST123A',
        offerid='00000000-0000-0000-0000-000000000001',
    )

    query = parse_qs(urlparse(requested[0]).query)
    assert 'itemid' not in query
    assert query['offerid'] == ['00000000-0000-0000-0000-000000000001']


def test_offer_page_requires_an_exact_asin_match():
    page = '''<script>
    var offerItems = [{"Id":"wrong-item","Asin":"B012345679"},
                      {"Id":"right-item","Asin":"B012345678"}];
    var offerId = "00000000-0000-0000-0000-000000000001";
    </script>'''

    assert woot._offer_ids_from_html(page, 'B012345678') == {
        'itemid': 'right-item', 'offerid': '00000000-0000-0000-0000-000000000001',
    }
    assert woot._offer_ids_from_html(page, 'B012345677') is None


def test_collect_reviews_resolves_offer_ids_from_asin(monkeypatch):
    from backend import external_effects
    monkeypatch.setattr(external_effects, 'assert_allowed', lambda _service: None)
    resolved = []
    requested = []

    def resolve(asin, **_kwargs):
        resolved.append(asin)
        return {'itemid': 'resolved-item', 'offerid': '00000000-0000-0000-0000-000000000001'}

    def fetch_filter(asin, star, sort, *, itemid=None, offerid=None, **_kwargs):
        requested.append((asin, itemid, offerid))
        return ([{'author': 'Reviewer', 'title': 'Useful', 'content': 'Works'}], 1)

    monkeypatch.setattr(woot, '_resolve_offer_ids', resolve)
    monkeypatch.setattr(woot, '_fetch_filter', fetch_filter)

    result = woot.collect_reviews('B012345678')

    assert resolved == ['B012345678']
    assert len(requested) == 20
    assert all(row == ('B012345678', 'resolved-item', '00000000-0000-0000-0000-000000000001')
               for row in requested)
    assert result['reviews'][0]['content'] == 'Works'


def test_resolver_checks_exact_asin_in_ranked_woot_pages(monkeypatch):
    monkeypatch.setattr(woot, '_amazon_product_title', lambda _asin, **_kwargs: 'Amazon Essentials Mens Pants')
    monkeypatch.setattr(woot, '_catalog_offer_urls', lambda **_kwargs: (
        ['https://sport.woot.com/offers/amazon-essentials-mens-pants',
         'https://home.woot.com/offers/amazon-essentials-mens-pants-2'], []))
    checked = []

    def fetch_offer(url, asin, **_kwargs):
        checked.append((url, asin))
        if url.endswith('-2'):
            return {'itemid': 'exact-item', 'offerid': '00000000-0000-0000-0000-000000000002'}
        return None

    monkeypatch.setattr(woot, '_fetch_offer_ids', fetch_offer)
    result = woot._resolve_offer_ids('B012345678')

    assert result == {'itemid': 'exact-item', 'offerid': '00000000-0000-0000-0000-000000000002'}
    assert checked
    assert all(row[1] == 'B012345678' for row in checked)


def test_resolver_reaches_exact_match_at_current_live_rank_177(monkeypatch):
    asin = "B0DS6HJYMN"
    title = "Dog Bed, Orthopedic Memory Cooling Foam (2X Base) 35x25x11in"
    target = "https://home.woot.com/offers/noah-paw-dog-cat-sofa-large-4"
    distractors = [
        f"https://home.woot.com/offers/orthopedic-memory-cooling-foam-dog-bed-{index}"
        for index in range(176)
    ]
    urls = [target, *distractors]
    assert woot._rank_offer_urls(title, urls).index(target) == 176

    monkeypatch.setattr(woot, "_ASIN_CACHE", {})
    monkeypatch.setattr(woot, "_amazon_product_title", lambda _asin, **_kwargs: title)
    monkeypatch.setattr(woot, "_catalog_offer_urls", lambda **_kwargs: (urls, []))
    checked = []

    def fetch_offer(url, _asin, **_kwargs):
        checked.append(url)
        if url == target:
            return {"itemid": "exact-item", "offerid": "00000000-0000-0000-0000-000000000003"}
        return None

    monkeypatch.setattr(woot, "_fetch_offer_ids", fetch_offer)

    result = woot._resolve_offer_ids(asin)

    assert result == {"itemid": "exact-item", "offerid": "00000000-0000-0000-0000-000000000003"}
    assert target in checked


def test_resolver_marks_only_complete_catalog_misses_as_fallback_eligible(monkeypatch):
    monkeypatch.setattr(woot, "_ASIN_CACHE", {})
    monkeypatch.setattr(woot, "_amazon_product_title", lambda _asin, **_kwargs: "Example Product")
    monkeypatch.setattr(woot, "_catalog_offer_urls", lambda **_kwargs: ([
        "https://electronics.woot.com/offers/example-product",
    ], []))
    monkeypatch.setattr(woot, "_fetch_offer_ids", lambda _url, _asin, **_kwargs: None)

    with pytest.raises(RuntimeError) as complete_miss:
        woot._resolve_offer_ids("B012345678")
    assert type(complete_miss.value).__name__ == "WootCatalogMiss"

    monkeypatch.setattr(woot, "_catalog_offer_urls", lambda **_kwargs: ([
        "https://electronics.woot.com/offers/example-product",
    ], ["sellout"]))
    with pytest.raises(RuntimeError) as incomplete:
        woot._resolve_offer_ids("B012345678")
    assert type(incomplete.value).__name__ == "WootCatalogIncomplete"


def test_catalog_lookup_marks_all_host_failures_as_incomplete(monkeypatch):
    monkeypatch.setattr(woot, "_CATALOG_CACHE", None)
    monkeypatch.setattr(woot, "WOOT_CATALOG_HOSTS", ("electronics", "home"))

    def fail_sitemap(_host, **_kwargs):
        raise OSError("catalog unavailable")

    monkeypatch.setattr(woot, "_sitemap_offer_urls", fail_sitemap)

    with pytest.raises(woot.WootCatalogIncomplete, match="无法读取 Woot 公开商品目录"):
        woot._catalog_offer_urls()


def test_resolver_marks_candidate_limit_exhaustion_as_incomplete(monkeypatch):
    monkeypatch.setattr(woot, "_ASIN_CACHE", {})
    monkeypatch.setattr(woot, "_amazon_product_title", lambda _asin, **_kwargs: "Example Product")
    urls = [f"https://electronics.woot.com/offers/example-product-{index}" for index in range(
        woot.MAX_RESOLVER_CANDIDATES + 1)]
    monkeypatch.setattr(woot, "_catalog_offer_urls", lambda **_kwargs: (urls, []))
    monkeypatch.setattr(woot, "_rank_offer_urls", lambda _title, candidates: candidates)
    monkeypatch.setattr(woot, "_fetch_offer_ids", lambda _url, _asin, **_kwargs: None)

    with pytest.raises(RuntimeError) as incomplete:
        woot._resolve_offer_ids("B012345678")

    assert type(incomplete.value).__name__ == "WootResolverSearchLimit"
