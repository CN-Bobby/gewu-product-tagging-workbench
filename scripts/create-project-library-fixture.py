"""Build a local, sanitized project-library fixture from the isolated dev copy.

This script never reads the PROD data directory. It selects only two named projects and the
source/Data-X/review/VOC artifacts required for repeatable UI acceptance. The destination must
not already exist; source files are only read, while fixture files and a minimal local database
are created beneath the destination.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid
from zipfile import BadZipFile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VERSION = 'project-library-v1'
FIXED_TIME = '2026-10-08T00:00:00+00:00'
EMAIL = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
PHONE = re.compile(r'(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)')
INLINE_SECRET = re.compile(r'(?i)\b(api[_-]?key|secret[_-]?key|bearer)\s*[:=]\s*[^\s,;"\']+')
SENSITIVE_HEADERS = re.compile(
    r'(?i)^(reviewer|reviewer[_ -]?(name|id|profile)|author[_ -]?name|username|user[_ -]?(name|id|profile)|'
    r'email|customer[_ -]?(name|id|email)|评论者|评论者姓名|用户名|用户姓名|用户编号|用户链接|用户邮箱|个人资料链接)$'
)
REDACT = '[已脱敏]'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def scrub_text(text: str, counts: dict[str, int]) -> str:
    text, counts['email_replacements'] = EMAIL.subn(REDACT, text)
    text, counts['phone_replacements'] = PHONE.subn(REDACT, text)
    text, counts['inline_secret_replacements'] = INLINE_SECRET.subn(r'\1=[已脱敏]', text)
    return text


def sanitize_file(source: Path, destination: Path, counts: dict[str, int]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in ('.xlsx', '.xlsm'):
        try:
            workbook = load_workbook(source, data_only=False, keep_vba=source.suffix.lower() == '.xlsm')
        except (OSError, BadZipFile) as exc:
            raise ValueError(f'无法检查验收工作簿: {source.name}') from exc
        for sheet in workbook.worksheets:
            sensitive_columns: set[int] = set()
            for cell in sheet[1]:
                header = str(cell.value or '').strip()
                if SENSITIVE_HEADERS.fullmatch(header):
                    sensitive_columns.add(cell.column)
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.column in sensitive_columns:
                        if cell.value is not None:
                            cell.value = None
                        continue
                    if isinstance(cell.value, str):
                        cell.value = scrub_text(cell.value, counts)
            if sensitive_columns:
                counts['identity_columns_removed'] += len(sensitive_columns)
        workbook.properties.creator = 'GeWu Local Acceptance Fixture'
        workbook.properties.lastModifiedBy = 'GeWu Local Acceptance Fixture'
        workbook.properties.title = 'GeWu Local Acceptance Fixture'
        workbook.save(destination)
        workbook.close()
        # Preserve the embedded-image acceptance case; a rewritten fixture may not silently drop it.
        checked = load_workbook(destination, read_only=False, data_only=True)
        image_count = sum(len(getattr(sheet, '_images', [])) for sheet in checked.worksheets)
        checked.close()
        if source.name == '商品打标结果.xlsx' and image_count == 0:
            raise ValueError('泳衣 Excel fixture lost its embedded images during sanitization')
    elif source.suffix.lower() in ('.html', '.htm', '.json', '.md', '.txt'):
        text = source.read_text(encoding='utf-8', errors='strict')
        destination.write_text(scrub_text(text, counts), encoding='utf-8')
    else:
        shutil.copyfile(source, destination)


def choose(rows: list[dict], kind: str, *, fixed: bool = False, output_exts=()) -> dict | None:
    candidates = [item for item in rows if item['kind'] == kind]
    if output_exts:
        candidates = [item for item in candidates if Path(item['name']).suffix.lower() in output_exts]
    if fixed:
        candidates = [item for item in candidates if 'fixed' in item['name'].lower()]
    return sorted(candidates, key=lambda item: (item['name'], item['relative_path']))[0] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-storage', type=Path, required=True,
                        help='Isolated dev/test storage copy containing workbench.db and projects/')
    parser.add_argument('--destination', type=Path, required=True,
                        help='New, empty fixture root, usually K:\\GeWuFixtures\\project-library-v1')
    args = parser.parse_args()
    source = args.source_storage.expanduser().resolve(strict=True)
    destination = args.destination.expanduser().resolve()
    if not (source / 'workbench.db').is_file() or not (source / 'projects').is_dir():
        parser.error('Source must be an isolated project-library storage copy.')
    if destination.exists():
        parser.error('Destination already exists; fixture versions are immutable and are never overwritten.')
    if source == destination or destination.is_relative_to(source):
        parser.error('Fixture destination may not be inside its source.')

    connection = sqlite3.connect(f'file:{source / "workbench.db"}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    projects = []
    for workspace in connection.execute('SELECT id,name FROM workspace ORDER BY name'):
        name = str(workspace['name'] or '')
        category = 'swimwear' if '泳衣' in name else 'dog-toy' if '狗狗发声玩具' in name else None
        if not category:
            continue
        rows = [dict(row) for row in connection.execute(
            'SELECT kind,source_type,name,relative_path FROM project_artifact WHERE workspace_id=?',
            (workspace['id'],))]
        selected = []
        for kind, kwargs in (
            ('source', {}), ('data_x', {'fixed': False}), ('reviews', {'fixed': category == 'swimwear'}),
            ('voc', {'output_exts': ('.html',)}), ('voc', {'output_exts': ('.xlsx',)}),
        ):
            item = choose(rows, kind, **kwargs)
            if item and item not in selected:
                selected.append(item)
        if category == 'dog-toy' and not any(item['kind'] == 'data_x' for item in selected):
            raise ValueError('Dog-toy fixture requires a saved Data-X version.')
        if category == 'swimwear' and not any(item['kind'] == 'reviews' and 'fixed' in item['name'].lower() for item in selected):
            raise ValueError('Swimwear fixture requires the fixed review workbook.')
        if not any(item['kind'] == 'source' for item in selected) or not any(
                item['kind'] == 'reviews' for item in selected) or not any(
                item['kind'] == 'voc' and Path(item['name']).suffix.lower() == '.html' for item in selected):
            raise ValueError(f'{category} fixture is missing source, review, or VOC report artifacts.')
        projects.append({'source_id': workspace['id'], 'source_name': name,
                         'category': category, 'artifacts': selected})
    connection.close()
    if {item['category'] for item in projects} != {'swimwear', 'dog-toy'}:
        parser.error('Expected both an isolated swimwear project and an isolated dog-toy project.')

    destination.mkdir(parents=True)
    seed = destination / 'seed-data'
    seed.mkdir()
    counts = {'email_replacements': 0, 'phone_replacements': 0,
              'inline_secret_replacements': 0, 'identity_columns_removed': 0}
    manifest_items = []
    workspace_records = []
    for project in sorted(projects, key=lambda item: item['category']):
        workspace_id = uuid.uuid5(uuid.NAMESPACE_URL, FIXTURE_VERSION + ':' + project['category']).hex
        workspace_name = '验收样本 · 泳衣选品档案' if project['category'] == 'swimwear' else '验收样本 · 狗狗发声玩具'
        project_root = seed / 'projects' / workspace_id
        folders = {'source': 'source/raw', 'data_x': 'data-x', 'reviews': 'reviews', 'voc': 'voc'}
        for relative in folders.values():
            (project_root / relative).mkdir(parents=True, exist_ok=True)
        artifacts = []
        for row in project['artifacts']:
            source_file = (source / 'projects' / project['source_id'] / row['relative_path']).resolve(strict=True)
            if not source_file.is_file() or not source_file.is_relative_to((source / 'projects' / project['source_id']).resolve()):
                raise ValueError('Source artifact path escaped its isolated project directory.')
            if source_file.name.lower() in ('.env', 'credentials.env', 'settings.json'):
                raise ValueError('Credential-bearing settings are forbidden in the fixture.')
            digest_name = hashlib.sha256(source_file.read_bytes()).hexdigest()[:12]
            display_name = Path(row['name']).name
            target_name = f'{Path(display_name).stem}-{digest_name}{source_file.suffix.lower()}'
            relative_path = folders[row['kind']] + '/' + target_name
            target = project_root / relative_path
            sanitize_file(source_file, target, counts)
            output_sha = sha256(target)
            artifact_id = uuid.uuid5(uuid.NAMESPACE_URL,
                f'{workspace_id}:{row["kind"]}:{display_name}:{output_sha}').hex
            source_types = {'source': 'dataset', 'data_x': 'data_x_snapshot',
                            'reviews': 'sorftime_mcp_review_export', 'voc': 'amazon_voc_output'}
            artifacts.append({'id': artifact_id, 'kind': row['kind'], 'source_type': source_types[row['kind']],
                              'source_id': f'{project["category"]}:{row["kind"]}:{output_sha[:12]}',
                              'name': display_name, 'relative_path': relative_path,
                              'sha256': output_sha, 'size': target.stat().st_size,
                              'metadata_json': {'fixture_version': FIXTURE_VERSION},
                              'created_at': FIXED_TIME})
            manifest_items.append({'path': f'seed-data/projects/{workspace_id}/{relative_path}',
                                   'sha256': output_sha, 'size': target.stat().st_size})
        workspace_records.append({'id': workspace_id, 'name': workspace_name,
                                  'created_at': FIXED_TIME, 'artifacts': artifacts})

    os.environ['TAG_STORAGE'] = str(seed)
    os.environ['TAG_CACHE'] = str(destination / 'cache')
    sys.path.insert(0, str(ROOT))
    from backend.db import Base, ProjectArtifact, Session, Workspace, engine, init_db
    init_db()
    with Session.begin() as session:
        for workspace in workspace_records:
            session.add(Workspace(id=workspace['id'], name=workspace['name'],
                                  created_at=workspace['created_at']))
        session.flush()
        for workspace in workspace_records:
            for item in workspace['artifacts']:
                session.add(ProjectArtifact(**item, workspace_id=workspace['id']))
    engine.dispose()
    database = seed / 'workbench.db'
    manifest_items.append({'path': 'seed-data/workbench.db', 'sha256': sha256(database), 'size': database.stat().st_size})
    manifest = {'fixture_version': FIXTURE_VERSION, 'created_at': FIXED_TIME,
                'purpose': 'local manual acceptance only; no external services',
                'source_scope': 'isolated dev project-library copy; PROD excluded',
                'projects': [{'id': item['id'], 'name': item['name'],
                              'artifact_count': len(item['artifacts'])} for item in workspace_records],
                'sanitization': counts,
                'files': sorted(manifest_items, key=lambda item: item['path'])}
    (destination / 'fixture-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'fixture_version': FIXTURE_VERSION, 'destination': str(destination),
                      'projects': len(workspace_records), 'files': len(manifest_items),
                      'sanitization': counts, 'manifest_sha256': sha256(destination / 'fixture-manifest.json')},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
