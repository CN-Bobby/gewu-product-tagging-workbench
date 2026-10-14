import pytest
from fastapi import HTTPException

from backend import project_library, templates
from backend.keyword_research import registry, runner
from backend.keyword_research.routes import run_project_link


def test_completed_keyword_run_can_be_linked_and_its_output_appears_in_project(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'ROOT', tmp_path / 'keyword-research')
    project = templates.create_project('Keyword output')
    run_id = registry.create({'groups': [], 'options': {}}, {'confirmed': True},
                             display_name='Older keyword run')
    output = registry.run_dir(run_id) / 'output' / 'keywords.xlsx'
    output.write_bytes(b'keyword workbook fixture')
    registry.write(run_id, status=registry.DONE,
                   artifacts=[{'name': output.name, 'bytes': output.stat().st_size,
                               'sha256': 'fixture'}])

    linked = run_project_link(run_id, {'workspace_id': project['id']})

    assert linked == {'run_id': run_id, 'workspace_id': project['id'],
                      'project_artifact_count': 1}
    state = registry.read(run_id)
    assert state['workspace_id'] == project['id']
    library = project_library.list_library(project['id'])
    keyword_items = next(folder['items'] for folder in library['folders']
                         if folder['kind'] == 'keywords')
    assert len(keyword_items) == 1
    assert keyword_items[0]['source_id'] == run_id
    assert keyword_items[0]['source_type'] == 'keyword_research'
    assert keyword_items[0]['name'] == 'keywords.xlsx'


def test_keyword_run_link_rejects_unknown_project(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'ROOT', tmp_path / 'keyword-research')
    run_id = registry.create({'groups': [], 'options': {}}, {'confirmed': True})
    with pytest.raises(HTTPException) as raised:
        run_project_link(run_id, {'workspace_id': 'f' * 32})
    assert raised.value.status_code == 400


def test_done_run_fallback_registers_only_final_deliverables(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'ROOT', tmp_path / 'keyword-research')
    project = templates.create_project('Keyword output fallback')
    run_id = registry.create({'groups': [], 'options': {}}, {'confirmed': True})
    output_dir = registry.run_dir(run_id) / 'output'
    final = output_dir / 'keywords.xlsx'
    final.write_bytes(b'final workbook')
    (output_dir / 'intermediate.xlsx').write_bytes(b'intermediate workbook')
    registry.write(run_id, status=registry.DONE, workspace_id=project['id'],
                   artifacts=[{'name': final.name, 'bytes': final.stat().st_size,
                               'sha256': 'fixture'}])

    runner._register_project_artifacts(run_id, registry.DONE)

    keyword_items = next(folder['items'] for folder in
                         project_library.list_library(project['id'])['folders']
                         if folder['kind'] == 'keywords')
    assert [item['name'] for item in keyword_items] == [final.name]
    assert keyword_items[0]['source_id'] == run_id
    assert keyword_items[0]['source_type'] == 'keyword_research'


def test_failed_run_fallback_stays_hidden_from_project_library(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'ROOT', tmp_path / 'keyword-research')
    project = templates.create_project('Failed keyword output')
    run_id = registry.create({'groups': [], 'options': {}}, {'confirmed': True})
    partial = registry.run_dir(run_id) / 'output' / 'partial.xlsx'
    partial.write_bytes(b'partial workbook')
    registry.write(run_id, status=registry.FAILED, workspace_id=project['id'])

    runner._register_project_artifacts(run_id, registry.FAILED)

    keyword_items = next(folder['items'] for folder in
                         project_library.list_library(project['id'])['folders']
                         if folder['kind'] == 'keywords')
    assert keyword_items == []
