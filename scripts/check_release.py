"""Read-only release gate. Never labels an unverified build as a release."""
import json
import subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[1]
v=json.loads((root/'VERSION.json').read_text(encoding='utf-8'))
errors=[]
if v['release_status']!='accepted':errors.append('milestone acceptance is not complete')
if subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip():errors.append('Git worktree is not clean')
for name in ('package.json','package-lock.json'):
    if json.loads((root/'frontend'/name).read_text())['version']!=v['version']:errors.append(name+' version mismatch')
for name in ('requirements.lock.txt','docs/DELIVERY_STATUS.md','docs/RELEASE_GOVERNANCE.md'):
    if not (root/name).is_file():errors.append(name+' missing')
print(json.dumps({'version':v['version'],'release_allowed':not errors,'errors':errors},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
