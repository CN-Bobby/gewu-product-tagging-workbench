"""Move superseded local builds and desktop artifacts into a hashed archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('version')
    parser.add_argument('--external-commit', required=True)
    parser.add_argument('--desktop', type=Path, required=True)
    args = parser.parse_args()
    current = json.loads((ROOT / 'VERSION.json').read_text(encoding='utf-8'))['version']
    if current == args.version:
        raise SystemExit('refusing to archive the current application version')
    source = (ROOT / 'releases' / args.version).resolve()
    archive_root = (ROOT / 'releases' / 'archive' / args.version).resolve()
    releases = (ROOT / 'releases').resolve()
    if releases not in source.parents or releases not in archive_root.parents:
        raise SystemExit('archive path escaped the releases directory')
    if not source.is_dir() or archive_root.exists():
        raise SystemExit('source is missing or archive already exists')
    archive_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(archive_root))

    builds = []
    for build in sorted(path for path in archive_root.iterdir() if path.is_dir()):
        artifacts = []
        for path in sorted(item for item in build.iterdir() if item.is_file()):
            artifacts.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)})
        builds.append({'build': build.name,
                       'kind': 'external_candidate' if build.name == args.external_commit[:12] else 'internal_candidate',
                       'artifacts': artifacts})

    desktop_archive = args.desktop.resolve() / '格物工作台历史版本' / args.version
    desktop_archive.mkdir(parents=True, exist_ok=True)
    moved_desktop = []
    for pattern in (f'格物工作台-{args.version}*', f'GeWuWorkbench-{args.version}*'):
        for path in args.desktop.glob(pattern):
            if path.is_file():
                target = desktop_archive / path.name
                if target.exists():
                    raise SystemExit(f'desktop archive already contains {target.name}')
                shutil.move(str(path), str(target))
                moved_desktop.append({'name': target.name, 'bytes': target.stat().st_size, 'sha256': sha256(target)})

    index = {'version': args.version, 'status': 'superseded', 'superseded_by': current,
             'external_commit': args.external_commit, 'archived_at': datetime.now(timezone.utc).isoformat(),
             'builds': builds, 'desktop_artifacts': moved_desktop}
    (archive_root / 'ARCHIVE_INDEX.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
    (desktop_archive / 'ARCHIVE_INDEX.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(archive_root), 'desktop_archive': str(desktop_archive),
                      'builds': len(builds), 'desktop_files': len(moved_desktop)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
