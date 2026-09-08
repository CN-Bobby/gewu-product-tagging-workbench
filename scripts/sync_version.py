"""Generate npm metadata from VERSION.json; --check never writes."""
import argparse
import json
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
version=json.loads((root/'VERSION.json').read_text(encoding='utf-8'))['version']
for name in ('package.json','package-lock.json'):
    path=root/'frontend'/name
    data=json.loads(path.read_text(encoding='utf-8'))
    mismatch=data['version']!=version or ('packages' in data and data['packages']['']['version']!=version)
    if args.check:
        if mismatch:raise SystemExit(f'{name} disagrees with VERSION.json; run scripts/sync_version.py')
    else:
        data['version']=version
        if 'packages' in data:data['packages']['']['version']=version
        path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
