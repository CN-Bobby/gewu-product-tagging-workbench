import json
from pathlib import Path
from sqlalchemy import select
from backend.build_info import VERSION, build_info
from backend.db import BatchJob, Session
from backend import text_facts

def test_version_is_consistent():
    root=Path(__file__).resolve().parents[1]
    assert VERSION==json.loads((root/'VERSION.json').read_text())['version']
    assert VERSION==json.loads((root/'frontend/package.json').read_text())['version']
    from backend.app import health
    assert health()['version']==VERSION
    assert health()['build_id'].startswith(VERSION+'+')
    # Health mirrors VERSION.json rather than naming one lifecycle state, so promoting a version
    # (pending_acceptance -> released) does not require editing this test.
    assert health()['release_status']==json.loads((root/'VERSION.json').read_text())['release_status']
    assert len(health()['source_sha256'])==64

def test_task_build_is_frozen_at_creation(dataset):
    job=text_facts.prepare(dataset['id'])
    with Session() as s:
        saved=s.get(BatchJob,job['id'])
        assert saved.spec['application_build']==build_info()
        assert saved.spec['application_build']['version']==VERSION


def test_packaged_identity_does_not_duplicate_file_inventory(tmp_path,monkeypatch):
    import hashlib
    from backend import build_info as module
    manifest={'version':VERSION,'commit':'test-build','files':{'runtime/file'+str(i):'f'*64 for i in range(3000)}}
    raw=json.dumps(manifest).encode();(tmp_path/'build-manifest.json').write_bytes(raw)
    monkeypatch.setattr(module,'ROOT',tmp_path);module.build_info.cache_clear()
    try:
        identity=module.build_info()
        assert 'files' not in identity and len(json.dumps(identity))<1000
        assert identity['manifest_sha256']==hashlib.sha256(raw).hexdigest()
    finally:module.build_info.cache_clear()


def test_desktop_launcher_keeps_runtime_cache_out_of_install_directory():
    root=Path(__file__).resolve().parents[1]
    source=(root/'scripts'/'DesktopLauncher.cs').read_text(encoding='utf-8')
    assert 'start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1"' in source
    assert '!Occupied(port)&&StorageReleased(data)' in source


def test_release_ledger_and_single_install_identity():
    root=Path(__file__).resolve().parents[1]
    version=json.loads((root/'VERSION.json').read_text(encoding='utf-8'))
    ledger=json.loads((root/'RELEASE_LEDGER.json').read_text(encoding='utf-8'))
    current=next(item for item in ledger['versions'] if item['version']==VERSION)
    assert current['status']==version['release_status']
    assert version['release_status'] in ('pending_acceptance','released')
    # Exactly one predecessor supersedes into this version, and it is the previous prerelease of the
    # same line: the assertion follows the version sequence instead of naming a literal, so bumping
    # the version does not require editing this test.
    previous=next(item for item in ledger['versions'] if item.get('superseded_by')==VERSION)
    assert previous['status']=='superseded'
    line,_,number=VERSION.partition('-beta.')
    assert number.isdigit() and int(number)>1
    assert previous['version']=='%s-beta.%d' % (line,int(number)-1)
    source=(root/'scripts'/'build_windows.py').read_text(encoding='utf-8')
    assert 'InstallDirRegKey HKCU "Software\\\\GeWuWorkbench" "InstallLocation"' in source
    assert 'Uninstall\\\\GeWuWorkbench' in source
    assert '格物工作台.lnk' in source
    assert "candidate_suffix='-candidate.'+commit[:12]" in source
    assert 'PageCallbacks DirectoryPagePre' in source
    assert 'IfFileExists "$INSTDIR\\\\Workbench.exe" 0 stable_repair' in source
    assert 'IfFileExists "$INSTDIR\\\\build-manifest.json" 0 stable_repair' in source
    assert 'IfFileExists "$INSTDIR\\\\.gewu-installing" 0 stable_invalid' in source
    assert '${DirState} "$INSTDIR" $0' in source
    assert '${DriveSpace} "$1" "/D=F /S=M" $0' in source
    assert 'File /r "APPDIR\\\\*"' in source
    assert 'SetOverwrite try' in source
    assert 'CopyFiles /SILENT' not in source
    assert 'UpgradeStage' not in source and 'UpgradeBackup' not in source
    assert '已保留或恢复原版本' not in source


def test_dev_build_identity_is_read_only_from_fields_the_build_writes():
    import re
    root=Path(__file__).resolve().parents[1]
    build=(root/'scripts'/'build_windows.py').read_text(encoding='utf-8')
    manifest_block=build.split('manifest={',1)[1].split('build-manifest.json',1)[0]
    written=set(re.findall(r"'([a-z_]+)':",manifest_block))
    launcher=(root/'scripts'/'dev-launcher.ps1').read_text(encoding='utf-8')
    reads=set(re.findall(r'\$manifest\.([a-z_]+)',launcher))
    assert reads<=written, sorted(reads-written)
    # build-test.ps1 fills the current pointer and prints from the line the build script emits.
    emitted=build.split("'output':str(out)",1)[1].split('raise SystemExit(0)',1)[0]
    emitted_keys=set(re.findall(r"'([a-z_]+)':",emitted))|{'output'}
    consumer=(root/'scripts'/'build-test.ps1').read_text(encoding='utf-8')
    consumed=set(re.findall(r'\$result\.([a-z_]+)',consumer))
    assert consumed<=emitted_keys, sorted(consumed-emitted_keys)
    # The UI shows the running build_id and marks a development or uncommitted build.
    page=(root/'frontend'/'src'/'main.jsx').read_text(encoding='utf-8')
    shown=set(re.findall(r'(?:buildInfo|runtimeStatus\.source)\??\.([a-z_]+)',page))
    assert {'build_id','build_mode','dirty'}<=shown<=written|{'version','release_status','commit'}


def test_customer_artifact_names_are_version_only():
    root=Path(__file__).resolve().parents[1]
    source=(root/'scripts'/'assemble_release.py').read_text(encoding='utf-8')
    assert "格物工作台-{args.version}-安装程序.exe" in source
    assert "格物工作台-{args.version}-客户分发包.zip" in source
    assert "version already promoted; increment the version" in source
    assert "item.get('superseded_by') == args.version" in source


def test_dev_build_directory_name_covers_every_worktree_change(tmp_path):
    # build_info()['source_sha256'] covers VERSION.json, requirements.txt, backend/, frontend/src/
    # and the shipped Product Tagging and Product Archive Research Skills. A tree whose sole change is scripts/desktop_bootstrap.py
    # (which does ship in the app) or frontend/package.json used to get the same build directory
    # name as the previous build, making the next build a refused overwrite instead of a new build.
    # The dev directory name must therefore be derived from the whole worktree.
    import ast,hashlib,os,subprocess
    root=Path(__file__).resolve().parents[1]
    source=(root/'scripts'/'build_windows.py').read_text(encoding='utf-8')
    # Run the real function body against a real repository: the script cannot be imported without
    # executing a build, and a text assertion alone would not show the digest discriminating.
    node=next(item for item in ast.parse(source).body if isinstance(item,ast.FunctionDef) and item.name=='worktree_digest')
    repo=tmp_path/'repo';(repo/'backend').mkdir(parents=True);(repo/'scripts').mkdir()
    def git(*arguments):return subprocess.run(['git',*arguments],cwd=repo,capture_output=True,text=True,check=True)
    git('init','-q');git('config','user.email','test@example.com');git('config','user.name','test')
    (repo/'backend'/'a.py').write_text('a=1\n');(repo/'scripts'/'desktop_bootstrap.py').write_text('x=1\n')
    git('add','-A');git('commit','-qm','base')
    namespace={'hashlib':hashlib,'os':os,'subprocess':subprocess,'ROOT':repo}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<worktree_digest>','exec'),namespace)
    digest=namespace['worktree_digest']
    clean=digest()
    assert digest()==clean, 'the same worktree must produce the same digest'
    # A change to a tracked file outside source_sha256's scope must move the digest.
    (repo/'scripts'/'desktop_bootstrap.py').write_text('x=2\n')
    changed=digest();assert changed!=clean,'a scripts/ change must move the digest'
    # An untracked file is part of the worktree state too.
    (repo/'scripts'/'dev-launcher.ps1').write_text('echo 1\n')
    untracked=digest();assert untracked!=changed,'a new untracked file must move the digest'
    # And so is a tracked change inside source_sha256's scope.
    (repo/'backend'/'a.py').write_text('a=2\n')
    assert digest()!=untracked,'a backend/ change must move the digest'
    # The directory name must be built from that digest rather than from source_sha256 alone.
    assert "worktree_sha256=worktree_digest()" in source
    assert "'-dirty.'+worktree_sha256[:8]" in source
    assert "build_info()['source_sha256'][:8]" not in source


def test_product_archive_research_skill_is_packaged_and_fingerprinted(tmp_path,monkeypatch):
    import ast
    from backend import build_info as module

    root=Path(__file__).resolve().parents[1]
    build_source=(root/'scripts'/'build_windows.py').read_text(encoding='utf-8')
    build_tree=ast.parse(build_source)
    copy_loop=next(node for node in build_tree.body if isinstance(node,ast.For) and
                   isinstance(node.target,ast.Name) and node.target.id=='directory' and
                   isinstance(node.iter,ast.Tuple) and
                   any(isinstance(item,ast.Constant) and item.value=='skills/product-tagging'
                       for item in node.iter.elts))
    copied_directories={ast.literal_eval(item) for item in copy_loop.iter.elts}
    assert 'skills/product-archive-research' in copied_directories

    (tmp_path/'VERSION.json').write_text(json.dumps({'version':VERSION}),encoding='utf-8')
    (tmp_path/'requirements.txt').write_text('runtime dependency\n',encoding='utf-8')
    for folder in ('backend','frontend/src','skills/product-tagging','skills/product-archive-research'):
        (tmp_path/folder).mkdir(parents=True)
    skill=tmp_path/'skills'/'product-archive-research'/'SKILL.md'
    skill.write_text('runtime skill version one\n',encoding='utf-8')

    monkeypatch.setattr(module,'ROOT',tmp_path)
    module.build_info.cache_clear()
    first=module.build_info()['source_sha256']
    skill.write_text('runtime skill version two\n',encoding='utf-8')
    module.build_info.cache_clear()
    second=module.build_info()['source_sha256']
    module.build_info.cache_clear()
    assert second!=first, 'changing the packaged Product Archive Skill must change source_sha256'
