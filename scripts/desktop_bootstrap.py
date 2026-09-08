"""Bundled runtime entrypoint: no system Python, Node or CLI dependency."""
import json,os,sys,sqlite3,shutil,traceback
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
DATA=Path(os.environ['TAG_STORAGE']);logs=DATA/'logs';logs.mkdir(parents=True,exist_ok=True)
sys.stdout=open(logs/'desktop.log','a',encoding='utf-8',buffering=1);sys.stderr=sys.stdout
try:
    manifest=json.loads((ROOT/'VERSION.json').read_text(encoding='utf-8'))
    previous=DATA/'installed-build.json'
    current=json.loads((ROOT/'build-manifest.json').read_text(encoding='utf-8'))
    if previous.exists() and json.loads(previous.read_text(encoding='utf-8')).get('commit')!=current.get('commit'):
        backup=DATA/'backups'/('upgrade-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));backup.mkdir(parents=True)
        if (DATA/'workbench.db').exists():
            with sqlite3.connect(DATA/'workbench.db') as src,sqlite3.connect(backup/'workbench.db') as dst:src.backup(dst)
        if (DATA/'datasets').exists():shutil.copytree(DATA/'datasets',backup/'datasets',symlinks=True)
        shutil.copy2(previous,backup/'installed-build.json')
    import uvicorn
    # Database migrations are additive and back up older structures before changes.
    from backend.db import init_db
    init_db()
    previous.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf-8')
    uvicorn.run('backend.app:app',host='127.0.0.1',port=int(os.environ['TAG_PORT']),access_log=False)
except Exception:
    traceback.print_exc();raise
