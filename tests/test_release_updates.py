import json
import pytest
from backend import release_updates as updates


def configure(tmp_path, monkeypatch, url=None):
    (tmp_path/'VERSION.json').write_text(json.dumps({'release_manifest_url':url}))
    monkeypatch.setattr(updates,'ROOT',tmp_path)
    monkeypatch.setattr(updates,'VERSION','0.5.0-alpha.1')


def test_semantic_order():
    versions=['0.4.1','0.5.0-alpha.1','0.5.0-alpha.2','0.5.0-beta.1','0.5.0-rc.1','0.5.0','0.10.0']
    assert sorted(reversed(versions),key=updates.ordering)==versions
    with pytest.raises(ValueError):updates.ordering('latest')


def test_unconfigured_never_contacts_network(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch)
    monkeypatch.setattr(updates,'fetch',lambda _:pytest.fail('unexpected request'))
    assert updates.check()['configured'] is False


def test_new_release_and_failure_are_distinct(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch,'https://publisher.example/releases.json')
    monkeypatch.setattr(updates,'fetch',lambda _:json.dumps({'version':'0.5.0-beta.1'}))
    assert updates.check()['available'] is True
    def fail(_):raise ValueError('unreachable')
    monkeypatch.setattr(updates,'fetch',fail)
    result=updates.check()
    assert result['status']=='更新查询失败' and 'available' not in result


def test_invalid_download_not_offered(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch,'https://publisher.example/releases.json')
    monkeypatch.setattr(updates,'fetch',lambda _:json.dumps({'version':'0.5.0-beta.1','installer_url':'http://127.0.0.1/setup.exe'}))
    assert updates.check()['status']=='更新查询失败'
