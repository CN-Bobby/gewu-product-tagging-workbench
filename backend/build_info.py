"""One version source; fingerprint code even when the development tree is dirty."""
import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / 'VERSION.json').read_text(encoding='utf-8'))['version']

@lru_cache(maxsize=1)
def build_info():
    release = ROOT / 'build-manifest.json'
    if release.exists():
        return json.loads(release.read_text(encoding='utf-8'))
    digest = hashlib.sha256()
    files = [ROOT / 'VERSION.json', ROOT / 'requirements.txt']
    for folder in ('backend', 'frontend/src', 'skills/product-tagging'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for p in sorted(files):
        digest.update(p.relative_to(ROOT).as_posix().encode())
        digest.update(b'\0' + p.read_bytes() + b'\0')
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, True
    return {'version': VERSION, 'commit': commit, 'dirty': dirty, 'source_sha256': digest.hexdigest(), 'release': False}
