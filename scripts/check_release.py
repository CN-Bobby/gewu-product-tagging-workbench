"""Read-only release gate. Never labels an unverified build as a release."""
import json
import subprocess
import argparse
import hashlib
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path);args=parser.parse_args()
v=json.loads((root/'VERSION.json').read_text(encoding='utf-8'))
errors=[]
if v['release_status']!='accepted':errors.append('milestone acceptance is not complete')
if subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip():errors.append('Git worktree is not clean')
for name in ('package.json','package-lock.json'):
    if json.loads((root/'frontend'/name).read_text())['version']!=v['version']:errors.append(name+' version mismatch')
for name in ('requirements.lock.txt','docs/DELIVERY_STATUS.md','docs/RELEASE_GOVERNANCE.md'):
    if not (root/name).is_file():errors.append(name+' missing')
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
if not args.evidence or not args.evidence.is_file():errors.append('current-commit release evidence is missing')
else:
    evidence=json.loads(args.evidence.read_text(encoding='utf-8'))
    if evidence.get('commit')!=commit or evidence.get('version')!=v['version']:errors.append('release evidence belongs to another build')
    for gate in ('automated','browser','real_model','performance','clean_windows','install','upgrade','rollback'):
        item=evidence.get('gates',{}).get(gate,{})
        if item.get('status')!='passed' or not item.get('artifact'):errors.append(gate+' evidence is not passed')
        else:
            artifact=Path(item['artifact'])
            if not artifact.is_absolute():artifact=args.evidence.parent/artifact
            if not artifact.is_file():errors.append(gate+' evidence artifact is missing')
            elif hashlib.sha256(artifact.read_bytes()).hexdigest()!=item.get('sha256'):errors.append(gate+' evidence checksum mismatch')
print(json.dumps({'version':v['version'],'release_allowed':not errors,'errors':errors},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
