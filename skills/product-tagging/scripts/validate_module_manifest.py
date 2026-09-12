"""Validate the embedded Skill package and its runtime-routing manifest."""
import json
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    errors=[];manifest_path=ROOT/'manifest.json'
    try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception as exc:
        print(json.dumps({'ok':False,'errors':[f'manifest: {exc}']},ensure_ascii=False));return 1
    if manifest.get('name')!='product-tagging':errors.append('manifest.name')
    if not re.fullmatch(r'\d+\.\d+\.\d+',str(manifest.get('version',''))):errors.append('manifest.version')
    modules=manifest.get('modules')
    if not isinstance(modules,dict) or not modules:errors.append('manifest.modules');modules={}
    for module_id,spec in modules.items():
        if not re.fullmatch(r'[a-z][a-z0-9_.]*',module_id):errors.append(f'module id: {module_id}')
        rel=spec.get('path') if isinstance(spec,dict) else None
        if not isinstance(rel,str):errors.append(f'module path: {module_id}');continue
        path=(ROOT/rel).resolve()
        if ROOT.resolve() not in path.parents:errors.append(f'module outside package: {module_id}')
        elif not path.is_file() or len(path.read_text(encoding='utf-8').strip())<40:errors.append(f'module missing/empty: {module_id}')
    if not set(manifest.get('default_modules',[]))<=set(modules):errors.append('default_modules contains unknown id')
    skill=(ROOT/'SKILL.md').read_text(encoding='utf-8')
    if not skill.startswith('---\nname: product-tagging\n'):errors.append('SKILL frontmatter')
    cases=[]
    for number,line in enumerate((ROOT/'examples'/'cases.jsonl').read_text(encoding='utf-8').splitlines(),1):
        try:cases.append(json.loads(line))
        except Exception as exc:errors.append(f'case {number}: {exc}')
    required={'apparel','drinkware','electronics','craft_components','toys_activity_kits'}
    if not required<={case.get('category') for case in cases}:errors.append('gold cases category coverage')
    result={'ok':not errors,'version':manifest.get('version'),'module_count':len(modules),'case_count':len(cases),'errors':errors}
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if not errors else 1

if __name__=='__main__':sys.exit(main())
