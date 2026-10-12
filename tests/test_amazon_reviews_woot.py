import pytest

from backend.amazon_reviews import service


def test_us_direct_asin_woot_does_not_require_sorftime(monkeypatch, tmp_path):
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    monkeypatch.setattr(service.sorftime_settings, 'connection',
                        lambda: (_ for _ in ()).throw(AssertionError('Sorftime must not be required')))
    monkeypatch.setattr(service, 'model_target', lambda *_args: ('https://model.invalid', 'secret', 'model'))
    monkeypatch.setattr(service, 'require_validated', lambda *_args: None)
    monkeypatch.setattr(service, '_dispatch_locked', lambda: None)

    job = service.create({
        'mode': 'asins', 'site': 'US', 'collection_source': 'woot',
        'asins': ['B012345678'], 'model': 'provider/model', 'credential_id': 'credential-id',
    })

    saved = service._read(job['id'])
    assert job['collection_source'] == 'woot'
    assert saved['asins'] == ['B012345678']
    assert saved['review_collection_contract']['source'] == 'Woot'


def test_woot_is_rejected_for_non_us_before_creating_a_task(monkeypatch, tmp_path):
    monkeypatch.setattr(service, 'ROOT', tmp_path)

    with pytest.raises(ValueError, match='仅支持 Amazon 美国站'):
        service.create({
            'mode': 'asins', 'site': 'GB', 'collection_source': 'woot',
            'asins': ['B012345678'], 'model': 'provider/model', 'credential_id': 'credential-id',
        })

    assert not list(tmp_path.iterdir())


def test_woot_category_mode_still_requires_sorftime(monkeypatch, tmp_path):
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    called = []
    monkeypatch.setattr(service.sorftime_settings, 'connection', lambda: called.append(True) or ('url', 'key'))
    monkeypatch.setattr(service, 'model_target', lambda *_args: ('https://model.invalid', 'secret', 'model'))
    monkeypatch.setattr(service, 'require_validated', lambda *_args: None)
    monkeypatch.setattr(service, '_dispatch_locked', lambda: None)

    job = service.create({
        'mode': 'node_asins', 'site': 'US', 'collection_source': 'woot',
        'node_id': '123', 'asins': ['B012345678'],
        'model': 'provider/model', 'credential_id': 'credential-id',
    })

    assert called == [True]
    assert job['collection_source'] == 'woot'
