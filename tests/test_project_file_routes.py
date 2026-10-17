from fastapi.testclient import TestClient

from backend.app import app
from backend.templates import create_project


def test_project_upload_move_download_trash_and_restore_routes():
    origin = create_project('Upload source')
    destination = create_project('Upload destination')
    payload = '<!doctype html><title>Saved report</title><p>safe content</p>'.encode()

    with TestClient(app) as client:
        uploaded = client.post(
            f"/api/projects/{origin['id']}/library/upload",
            data={'kind': 'other'}, files={'file': ('report.html', payload, 'text/html')})
        assert uploaded.status_code == 200, uploaded.text
        artifact = uploaded.json()
        assert artifact['source_type'] == 'manual_upload'
        assert artifact['kind'] == 'other'

        renamed = client.patch(
            f"/api/projects/{origin['id']}/library/{artifact['id']}",
            json={'name': 'VOC summary.html', 'kind': 'voc', 'workspace_id': destination['id']})
        assert renamed.status_code == 200, renamed.text
        moved = renamed.json()
        assert moved['workspace_id'] == destination['id']
        assert moved['name'] == 'VOC summary.html'
        assert moved['source_type'] == artifact['source_type']
        assert moved['source_id'] == artifact['source_id']
        assert moved['sha256'] == artifact['sha256']

        download = client.get(
            f"/api/projects/{destination['id']}/artifacts/{artifact['id']}/download")
        assert download.status_code == 200
        assert download.content == payload

        deleted = client.delete(
            f"/api/projects/{destination['id']}/library/{artifact['id']}")
        assert deleted.status_code == 200
        assert deleted.json()['deleted_at']
        assert client.get(f"/api/projects/{destination['id']}/library").json()['folders'][3]['items'] == []
        trash = client.get(f"/api/projects/{destination['id']}/trash")
        assert [item['id'] for item in trash.json()['items']] == [artifact['id']]

        restored = client.post(
            f"/api/projects/{destination['id']}/trash/{artifact['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()['deleted_at'] is None
        assert client.get(
            f"/api/projects/{destination['id']}/artifacts/{artifact['id']}/download").content == payload


def test_project_upload_rejects_unsupported_extensions():
    project = create_project('Upload validation')
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/library/upload",
            data={'kind': 'other'}, files={'file': ('notes.txt', b'content', 'text/plain')})
    assert response.status_code == 400
