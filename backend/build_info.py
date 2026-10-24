"""One version source; fingerprint code even when the development tree is dirty."""
import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_DATA = json.loads((ROOT / 'VERSION.json').read_text(encoding='utf-8'))
VERSION = VERSION_DATA['version']

@lru_cache(maxsize=1)
def build_info():
    release = ROOT / 'build-manifest.json'
    if release.exists():
        raw=release.read_bytes()
        manifest=json.loads(raw)
        # Keep the complete file inventory in the release artifact, not every cell/job/event.
        result={k:v for k,v in manifest.items() if k!='files'}
        result['manifest_sha256']=hashlib.sha256(raw).hexdigest()
        return result
    digest = hashlib.sha256()
    files = [ROOT / 'VERSION.json', ROOT / 'requirements.txt']
    for folder in ('backend', 'frontend/src', 'skills/product-tagging', 'skills/product-archive-research'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for p in sorted(files):
        digest.update(p.relative_to(ROOT).as_posix().encode())
        digest.update(b'\0' + p.read_bytes() + b'\0')
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True))
    except (OSError, subprocess.CalledProcessError):
        commit, branch, dirty = None, None, True
    short_commit = commit[:12] if commit else 'untracked'
    return {'version': VERSION, 'build_id': f'{VERSION}+{short_commit}', 'branch': branch,
            'release_status': VERSION_DATA.get('release_status', 'unknown'),
            'commit': commit, 'dirty': dirty, 'source_sha256': digest.hexdigest(), 'release': False}
