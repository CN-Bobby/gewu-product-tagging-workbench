"""Resolve only the app's installed dependency closure, not unrelated global packages."""
from importlib.metadata import distribution
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
pending=[Requirement(n) for n in ['fastapi','uvicorn','sqlalchemy','openpyxl','pillow','python-multipart','httpx','python-dotenv','mcp',
                                 'numpy','pandas','statsmodels']]
found={};visited={}
while pending:
    requested=pending.pop();dist=distribution(requested.name);key=canonicalize_name(dist.metadata['Name'])
    extras=set(requested.extras)|{''}
    if key in found and extras<=visited[key]:continue
    visited[key]=visited.get(key,set())|extras
    found[key]=dist.version
    for raw in dist.requires or []:
        requirement=Requirement(raw)
        if requirement.marker is None or any(requirement.marker.evaluate({'extra':extra}) for extra in visited[key]):pending.append(requirement)
(ROOT/'requirements-runtime.lock.txt').write_text(''.join(f'{k}=={v}\n' for k,v in sorted(found.items())),encoding='utf-8')
print(f'{len(found)} runtime packages locked')
