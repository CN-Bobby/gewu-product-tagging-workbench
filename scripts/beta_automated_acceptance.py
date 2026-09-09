"""Persist reproducible automated gate evidence for the current clean commit."""
import argparse,hashlib,json,subprocess,time
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
version=json.loads((root/'VERSION.json').read_text(encoding='utf-8'))['version']
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()

def run(name,command,cwd=root):
    started=time.perf_counter();completed=subprocess.run(command,cwd=cwd,text=True,capture_output=True,encoding='utf-8',errors='replace')
    item={'name':name,'command':command,'returncode':completed.returncode,'seconds':time.perf_counter()-started,
          'stdout':completed.stdout,'stderr':completed.stderr}
    if completed.returncode:raise RuntimeError(json.dumps(item,ensure_ascii=False))
    return item

report={'version':version,'commit':commit,'started_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'checks':[]}
report['checks'].append(run('version sync',[str(root/'.venv/Scripts/python.exe'),'scripts/sync_version.py','--check']))
report['checks'].append(run('pytest',[str(root/'.venv/Scripts/python.exe'),'-m','pytest','-q']))
report['checks'].append(run('frontend build',['npm.cmd','run','build'],root/'frontend'))
report['checks'].append(run('diff check',['git','diff','--check']))
dirty=subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
if dirty:raise RuntimeError('Automated evidence requires a clean worktree: '+dirty)
report['status']='passed';report['completed_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
path=args.out/'automated-report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(path),'version':version,'commit':commit,'status':'passed','sha256':hashlib.sha256(path.read_bytes()).hexdigest()},ensure_ascii=False))
