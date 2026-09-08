"""Download pinned build tools and wheels; verify bytes before extraction or execution."""
import hashlib,io,json,subprocess,sys,tarfile,zipfile
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1];out=ROOT/'storage/build-tools';out.mkdir(parents=True,exist_ok=True)
pins=json.loads((ROOT/'build-tools.lock.json').read_text(encoding='utf-8'))
for name,pin in pins.items():
    destination=out/name
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest()==pin['sha256']:continue
    url=pin.get('url') or pin['npm_archive']
    response=httpx.get(url,follow_redirects=True,timeout=120);response.raise_for_status();data=response.content
    if pin.get('npm_archive'):
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            members=[m for m in archive.getmembers() if m.isfile() and m.name.endswith('/bin/reasonix.exe')]
            if len(members)!=1:raise RuntimeError('Unexpected Reasonix archive layout')
            data=archive.extractfile(members[0]).read()
    if hashlib.sha256(data).hexdigest()!=pin['sha256']:raise RuntimeError('Checksum mismatch: '+name)
    destination.write_bytes(data)
with zipfile.ZipFile(out/'nsis-3.12.zip') as archive:archive.extractall(out)
subprocess.run([sys.executable,'-m','pip','download','--require-hashes','--only-binary=:all:',
               '-r',str(ROOT/'requirements-runtime-hashed.txt'),'--dest',str(out/'wheels')],check=True)
