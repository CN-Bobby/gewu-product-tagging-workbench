"""Exercise real startup scripts on isolated storage and ports, without model calls."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'storage' / 'release-verification' / ('startup-' + uuid.uuid4().hex)
OUT.mkdir(parents=True)
os.environ['TAG_STORAGE'] = str(OUT)
os.environ.pop('DATABASE_URL', None)
sys.path.insert(0, str(ROOT))
from backend.db import init_db, Session, Workspace, Dataset, BatchJob


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def script(name, chosen, storage=OUT, success=True):
    # Files avoid inherited pipe handles keeping communicate() open after PowerShell exits.
    log = OUT / ('command-' + uuid.uuid4().hex + '.log')
    with log.open('wb') as output:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-File',
            str(ROOT / 'scripts' / name), '-Port', str(chosen), '-Storage', str(storage)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output, timeout=55)
    assert (result.returncode == 0) == success, log.read_bytes().decode(errors='replace')
    return result.returncode


init_db()
with Session.begin() as s:
    workspace = Workspace(name='启动验收测试'); s.add(workspace); s.flush()
    dataset = Dataset(workspace_id=workspace.id, name='启动验收测试', version=1,
                      headers=[], mapping={}); s.add(dataset); s.flush()
    task = BatchJob(dataset_id=dataset.id, status='running',
                    spec={'columns': [], 'expected': {'finished': {}, 'pending': {}}},
                    completed=['finished'], errors={'pending': '验收用待恢复项'})
    s.add(task); s.flush(); jid = task.id

chosen = port()
checks = []
try:
    script('start.ps1', chosen)
    health = json.load(urlopen(f'http://127.0.0.1:{chosen}/api/health', timeout=3))
    assert health['storage'] == str(OUT)
    with Session() as s:
        task = s.get(BatchJob, jid)
        assert task.status == 'interrupted' and task.completed == ['finished']
        assert task.errors == {'pending': '验收用待恢复项'}
    checks.append('cold start retains checkpoint and exposes interrupted task')
    script('start.ps1', chosen)
    checks.append('duplicate start reuses matching service')
    script('start.ps1', chosen, OUT / 'other', success=False)
    script('stop.ps1', chosen, OUT / 'other', success=False)
    checks.append('different storage cannot reuse or stop the service')
    with Session.begin() as s:
        s.get(BatchJob, jid).status = 'running'
    # Only terminate the listener on the unique port allocated by this test.
    code = f"$p=Get-NetTCPConnection -LocalPort {chosen} -State Listen; Stop-Process -Id $p.OwningProcess -Force"
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], check=True, timeout=15)
    script('start.ps1', chosen)
    with Session() as s:
        task = s.get(BatchJob, jid)
        assert task.status == 'interrupted' and task.completed == ['finished']
    checks.append('forced exit and restart preserve completed items; resume remains explicit')
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0)); occupied.listen()
        other_port = occupied.getsockname()[1]
        script('start.ps1', other_port, success=False)
        assert occupied.fileno() >= 0
    checks.append('unrelated listening port rejected without termination')
finally:
    script('stop.ps1', chosen)

(OUT / 'report.json').write_text(json.dumps({'health': health, 'checks': checks,
    'model_requests': 0, 'limit': 'Checkpoint recovery tested; real model resume not exercised.'},
    ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'report': str(OUT / 'report.json'), 'checks': checks}, ensure_ascii=True))
