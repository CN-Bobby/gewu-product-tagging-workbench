"""Assemble the documented GitHub Release assets from a built app directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def zip_tree(source: Path, target: Path, prefix: str) -> None:
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.write(path, f'{prefix}/{path.relative_to(source).as_posix()}')


def zip_assets(build_dir: Path, target: Path) -> None:
    excluded = {target.name, target.name + '.sha256', 'app', 'PROMOTION.json'}
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(build_dir.iterdir()):
            if path.name in excluded or '-candidate.' in path.name or not path.is_file():
                continue
            archive.write(path, f'GeWuWorkbench-{build_dir.parent.name}/{path.name}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    build_dir = args.build_dir.resolve()
    if not build_dir.is_dir() or not (build_dir / 'app').is_dir():
        raise SystemExit(f'build directory is incomplete: {build_dir}')
    evidence = args.evidence.resolve()
    if not evidence.is_file():
        raise SystemExit(f'evidence file is missing: {evidence}')

    ledger = json.loads((ROOT / 'RELEASE_LEDGER.json').read_text(encoding='utf-8'))
    entry = next((item for item in ledger.get('versions', []) if item.get('version') == args.version), None)
    if not entry or entry.get('status') != 'pending_acceptance':
        raise SystemExit('release ledger has no pending candidate for this version')
    existing = [path for path in (ROOT / 'releases' / args.version).glob('*/PROMOTION.json') if path.parent != build_dir]
    if existing:
        raise SystemExit(f'version already promoted; increment the version instead of overwriting: {existing[0]}')
    manifest = json.loads((build_dir / 'release-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != args.version or manifest.get('release_status') != 'pending_acceptance':
        raise SystemExit('build manifest and requested promotion disagree')
    candidates = list(build_dir.glob(f'格物工作台-{args.version}-candidate.*-安装程序.exe'))
    if len(candidates) != 1:
        raise SystemExit('expected exactly one internal candidate installer')
    customer_installer = build_dir / f'格物工作台-{args.version}-安装程序.exe'
    if customer_installer.exists():
        raise SystemExit('customer installer already exists; refusing to overwrite')
    shutil.copy2(candidates[0], customer_installer)

    mapping = {
        ROOT / 'docs' / 'CUSTOMER_GUIDE.md': '客户使用说明.md',
        ROOT / 'docs' / 'UPGRADE_ROLLBACK.md': '升级与回退说明.md',
        ROOT / 'docs' / 'KNOWN_ISSUES.md': '已知问题.md',
        ROOT / 'docs' / f'ACCEPTANCE-{args.version}.md': '验收报告.md',
        ROOT / '本机工作台入口与操作规则.md': '本机工作台入口与操作规则.md',
    }
    for source, name in mapping.items():
        if not source.is_file():
            raise SystemExit(f'release source is missing: {source}')
        shutil.copy2(source, build_dir / name)
    shutil.copy2(evidence, build_dir / 'ACCEPTANCE-RESULTS.json')

    licenses = build_dir / 'THIRD-PARTY-LICENSES.zip'
    zip_tree(build_dir / 'app' / 'THIRD_PARTY_LICENSES', licenses, 'THIRD_PARTY_LICENSES')
    evidence_zip = build_dir / 'acceptance-evidence.zip'
    with zipfile.ZipFile(evidence_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(evidence, f'evidence/{evidence.name}')
        evidence_dir = evidence.parent
        for path in sorted(evidence_dir.iterdir()):
            if path == evidence or not path.is_file() or path.suffix.lower() not in {'.json', '.txt', '.md'}:
                continue
            archive.write(path, f'evidence/{path.name}')

    distribution = build_dir / f'格物工作台-{args.version}-客户分发包.zip'
    sidecar = distribution.with_name(distribution.name + '.sha256')
    for path in (distribution, sidecar):
        if path.exists():
            path.unlink()

    excluded = {build_dir / 'SHA256SUMS.txt', build_dir / 'app', distribution, sidecar}
    lines = []
    for path in sorted(build_dir.iterdir()):
        if path in excluded or '-candidate.' in path.name or not path.is_file():
            continue
        lines.append(f'{sha256(path)}  {path.name}\n')
    (build_dir / 'SHA256SUMS.txt').write_text(''.join(lines), encoding='utf-8')

    # The package contains the final checksum file. Its own checksum is kept in
    # the sidecar so the checksum file inside the package is not circular.
    zip_assets(build_dir, distribution)
    sidecar.write_text(f'{sha256(distribution)}  {distribution.name}\n', encoding='utf-8')
    promotion = {
        'version': args.version,
        'build_id': manifest.get('build_id'),
        'commit': manifest.get('commit'),
        'status': 'pending_acceptance',
        'supersedes': '0.5.0-beta.1',
        'installer': {'name': customer_installer.name, 'sha256': sha256(customer_installer)},
        'distribution': {'name': distribution.name, 'sha256': sha256(distribution)},
        'tag_created': False,
        'github_release_created': False,
    }
    (build_dir / 'PROMOTION.json').write_text(json.dumps(promotion, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'build_dir': str(build_dir), 'distribution': str(distribution), 'sha256': sha256(distribution)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
