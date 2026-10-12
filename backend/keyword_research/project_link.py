"""Bridge completed keyword research outputs into the project file library."""
from pathlib import Path

from .. import project_files
from . import registry


def sync_run_artifacts(run_id: str, workspace_id: str | None) -> list[dict]:
    if not workspace_id:
        return []
    state = registry.read(run_id)
    if state is None:
        raise ValueError('关键词运行不存在')
    output = registry.run_dir(run_id) / 'output'
    linked = []
    for artifact in state.get('artifacts') or []:
        name = Path(str(artifact.get('name') or '')).name
        source = output / name
        if source.suffix.casefold() != '.xlsx' or not source.is_file():
            continue
        linked.append(project_files.register_file(
            workspace_id, 'keywords', source, source_type='keyword_research',
            source_id=run_id, name=name,
            metadata={'run_id': run_id, 'source_task_url': f'#/keyword/run/{run_id}',
                      'sha256': artifact.get('sha256')}))
    return linked
