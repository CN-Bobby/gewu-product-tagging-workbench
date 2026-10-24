import io
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from backend import batch, importer, text_facts
from backend.db import Asset, BatchJob, Product, Session, obj, require
from backend.import_image_download import ImageDownloadResult
from tests.test_import_enrichment import workbook_bytes


def _image_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (3, 3), 'green').save(buffer, format='PNG')
    return buffer.getvalue()


def test_image_button_job_only_downloads_missing_images_without_model(monkeypatch):
    from backend import import_image_download

    dataset = importer.import_file(workbook_bytes(), 'image-only.xlsx')
    calls = []
    monkeypatch.setattr(batch, 'request_json', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('model called')))
    monkeypatch.setattr(import_image_download, 'download_image',
                        lambda url, **_kwargs: (calls.append(url) or ImageDownloadResult(data=_image_bytes(), content_type='image/png')))
    job = text_facts.prepare_images(dataset['id'])
    assert job['spec']['kind'] == 'image_download'
    assert job['spec']['expected'] == {}
    assert len(job['spec']['image_assets']) == 1
    assert text_facts.prepare_images(dataset['id'])['id'] == job['id']
    batch.active[job['id']] = threading.Event()
    batch.execute(job['id'])
    with Session() as session:
        stored = obj(require(session, BatchJob, job['id']))
        product = session.scalar(select(Product).where(Product.dataset_id == dataset['id']))
        asset = session.scalar(select(Asset).where(Asset.product_id == product.id, Asset.url.is_not(None)))
        assert stored['status'] == 'completed'
        assert stored['metrics']['images_completed'] == 1
        assert stored['metrics']['requests'] == 0
        assert asset.path and Path(asset.path).is_file()
    assert len(calls) == 1
    assert text_facts.prepare_images(dataset['id'])['status'] == 'completed'


def test_image_button_endpoint_creates_only_image_job(monkeypatch):
    from backend.app import app

    dataset = importer.import_file(workbook_bytes(), 'image-endpoint.xlsx')
    launched = []
    monkeypatch.setattr(batch, 'launch', lambda jid: launched.append(jid))
    with TestClient(app) as client:
        response = client.post(f"/api/datasets/{dataset['id']}/images/download")
    assert response.status_code == 200
    assert response.json()['spec']['kind'] == 'image_download'
    assert response.json()['metrics']['images_expected'] == 1
    assert launched == [response.json()['id']]


def test_text_extraction_does_not_overlap_running_image_download():
    import pytest

    dataset = importer.import_file(workbook_bytes(), 'no-overlap.xlsx')
    text_facts.prepare_images(dataset['id'])
    with pytest.raises(ValueError, match='图片下载任务仍在运行'):
        text_facts.prepare(dataset['id'])


def test_image_download_retries_one_transient_failure_only(monkeypatch):
    from backend import import_image_download

    calls = []
    def fake(url, **_kwargs):
        calls.append(url)
        return (ImageDownloadResult(error_code='timeout', error_message='下载图片超时')
                if len(calls) == 1 else ImageDownloadResult(data=_image_bytes(), content_type='image/png'))
    monkeypatch.setattr(import_image_download, 'download_image', fake)
    result = import_image_download.download_with_retry('https://images.example/item.png', delay=0)
    assert result.ok
    assert len(calls) == 2


def test_followup_image_job_settles_old_image_only_failure(monkeypatch):
    from backend import import_image_download

    dataset = importer.import_file(workbook_bytes(), 'followup-images.xlsx')
    with Session.begin() as session:
        asset = session.scalar(select(Asset).join(Product, Asset.product_id == Product.id)
                               .where(Product.dataset_id == dataset['id'], Asset.url.is_not(None)))
        old = BatchJob(id='old-images-only-failure', dataset_id=dataset['id'], status='partial',
                       spec={'kind': 'text_facts', 'expected': {'done-product': {}},
                             'image_assets': {asset.id: {'url': asset.url}}},
                       completed=['done-product'], errors={'image:' + asset.id: '下载图片超时'},
                       metrics={'images_failed': 1})
        session.add(old)
    monkeypatch.setattr(import_image_download, 'download_image',
                        lambda _url, **_kwargs: ImageDownloadResult(data=_image_bytes(), content_type='image/png'))
    job = text_facts.prepare_images(dataset['id'])
    batch.active[job['id']] = threading.Event()
    batch.execute(job['id'])
    with Session() as session:
        prior = obj(require(session, BatchJob, old.id))
        assert prior['status'] == 'resolved'
        assert prior['metrics']['resolved_by_image_job_id'] == job['id']
        assert prior['errors']['image:' + asset.id] == '下载图片超时'


def test_no_missing_images_settles_prior_image_failure_after_restart(monkeypatch):
    from backend import import_image_download

    dataset = importer.import_file(workbook_bytes(), 'already-downloaded-images.xlsx')
    monkeypatch.setattr(import_image_download, 'download_image',
                        lambda _url, **_kwargs: ImageDownloadResult(data=_image_bytes(), content_type='image/png'))
    job = text_facts.prepare_images(dataset['id'])
    batch.active[job['id']] = threading.Event()
    batch.execute(job['id'])
    with Session.begin() as session:
        asset = session.scalar(select(Asset).join(Product, Asset.product_id == Product.id)
                               .where(Product.dataset_id == dataset['id'], Asset.url.is_not(None)))
        session.add(BatchJob(id='old-images-after-restart', dataset_id=dataset['id'], status='partial',
                             spec={'kind': 'text_facts', 'expected': {},
                                   'image_assets': {asset.id: {'url': asset.url}}},
                             errors={'image:' + asset.id: '历史下载超时'}))
    assert text_facts.prepare_images(dataset['id'])['status'] == 'completed'
    with Session() as session:
        assert require(session, BatchJob, 'old-images-after-restart').status == 'resolved'
