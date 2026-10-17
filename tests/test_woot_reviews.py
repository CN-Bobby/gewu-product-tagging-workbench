import json

from backend.amazon_reviews import woot


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
    progress = []

    result = woot.collect_reviews('B012345678', progress.append)

    assert len(calls) == 20
    assert all(timeout == 15 for _, timeout in calls)
    assert len(result['reviews']) == 1
    assert result['request_count'] == 20
    assert result['reviews'][0]['review_date'] == '2025-06-25'
    assert result['reviews'][0]['star_rating'] == 5
    assert len(progress) == 20
    assert progress[0].startswith('Woot 组合 1/20')
    assert progress[-1].startswith('Woot 组合 20/20')
    assert sleeps == []


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
