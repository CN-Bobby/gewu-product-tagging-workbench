"""Build from a clean commit with pinned wheels, embedded Python and native Reasonix.

``--dev`` produces a runnable development directory under ``builds/`` with no installer and no
release gate. ``--portable`` produces a frozen, manually reviewable portable candidate ZIP."""
import argparse,hashlib,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();build_kind=parser.add_mutually_exclusive_group()
build_kind.add_argument('--candidate',action='store_true')
build_kind.add_argument('--dev',action='store_true',help='development build: skip the release gate and the installer, write to builds/')
build_kind.add_argument('--portable',action='store_true',help='build a clean, commit-pinned portable candidate ZIP under builds/')
parser.add_argument('--allow-dirty',action='store_true',help='dev only: build from a dirty worktree and record dirty=true')
parser.add_argument('--evidence',type=Path);args=parser.parse_args()


def worktree_digest():
    """Fingerprint every worktree change git reports, for development build directory naming.

    ``build_info()['source_sha256']`` covers the shipped runtime sources only, so a tree whose sole
    change is ``scripts/desktop_bootstrap.py`` (which does ship) or ``frontend/package.json`` used to
    yield the same directory name and turn the next build into a refused overwrite. This is a
    development naming input; it never replaces ``source_sha256`` in a manifest, health or job spec.
    """
    digest=hashlib.sha256()
    digest.update(subprocess.check_output(['git','diff','HEAD','--binary'],cwd=ROOT))
    others=subprocess.check_output(['git','ls-files','--others','--exclude-standard','-z'],cwd=ROOT)
    for name in sorted(x for x in others.split(b'\0') if x):
        digest.update(name+b'\0')
        path=ROOT/os.fsdecode(name)
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b'absent')
    return digest.hexdigest()


version=json.loads((ROOT/'VERSION.json').read_text(encoding='utf-8'))
ledger=json.loads((ROOT/'RELEASE_LEDGER.json').read_text(encoding='utf-8'))
ledger_entries=[item for item in ledger.get('versions',[]) if item.get('version')==version['version']]
ledger_entry=ledger_entries[0] if len(ledger_entries)==1 else None
dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
if args.dev:
    # A development build is not a release: no ledger gate, no acceptance gate, no installer.
    # It may come from a dirty tree, but then the fact is recorded instead of hidden.
    if dirty and not args.allow_dirty:
        raise SystemExit('Build requires clean Git worktree (pass --allow-dirty for a development build)')
else:
    if not ledger_entry or ledger_entry.get('status')!=version.get('release_status'):
        raise SystemExit('VERSION.json and RELEASE_LEDGER.json disagree')
    if dirty:raise SystemExit('Build requires clean Git worktree')
    if args.portable and (args.allow_dirty or len(ledger_entries)!=1 or
                          version.get('release_status')!='pending_acceptance' or
                          ledger_entry.get('status')!='pending_acceptance' or
                          ledger_entry.get('superseded_by')):
        raise SystemExit('Portable candidates require a clean tree and exactly one, unsuperseded pending_acceptance ledger entry matching VERSION.json')
    if not args.candidate and not args.portable:
        gate=[sys.executable,str(ROOT/'scripts/check_release.py')]
        if args.evidence:gate.extend(['--evidence',str(args.evidence)])
        subprocess.run(gate,check=True)
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
branch=subprocess.check_output(['git','rev-parse','--abbrev-ref','HEAD'],cwd=ROOT,text=True).strip()
if args.dev:
    sys.path.insert(0,str(ROOT));from backend.build_info import build_info
    worktree_sha256=worktree_digest() if dirty else None
    # Two dirty trees can share HEAD, so the directory name carries the worktree digest as well.
    # build_id itself stays <version>+<commit> per RELEASE_LEDGER policy.
    directory=version['version']+'+'+commit[:12]+('-dirty.'+worktree_sha256[:8] if dirty else '')
    out=ROOT/'builds'/directory
elif args.portable:
    worktree_sha256=None
    out=ROOT/'builds'/(version['version']+'-candidate.'+commit[:12])
else:
    worktree_sha256=None
    out=ROOT/'releases'/version['version']/commit[:12]
if out.exists():
    raise SystemExit('Refusing to overwrite an existing build directory: '+str(out)+
        ' - this worktree state was already built. Remove that directory to rebuild it.')
app=out/'app';app.mkdir(parents=True)
tools=ROOT/'storage/build-tools';pins=json.loads((ROOT/'build-tools.lock.json').read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for name,item in pins.items():
    path=Path(item['local']) if item.get('local') else tools/name
    if sha(path)!=item['sha256']:raise SystemExit('Build input checksum mismatch: '+name)
for directory in ('backend','skills/product-tagging','skills/product-archive-research','docs'):
    shutil.copytree(ROOT/directory,app/directory,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
(app/'scripts').mkdir()
shutil.copy2(ROOT/'scripts/desktop_bootstrap.py',app/'scripts/desktop_bootstrap.py')
shutil.copy2(ROOT/'scripts/restore_backup.py',app/'scripts/restore_backup.py')
for name in ('VERSION.json','RELEASE_LEDGER.json','requirements-runtime.lock.txt','requirements-runtime-hashed.txt'):
    shutil.copy2(ROOT/name,app/name)
runtime=app/'runtime'
with zipfile.ZipFile(tools/'python-3.14.3-embed-amd64.zip') as archive:archive.extractall(runtime)
(runtime/'python314._pth').write_text('python314.zip\n.\nLib/site-packages\n..\nimport site\n',encoding='utf-8')
subprocess.run([sys.executable,'-m','pip','install','--no-index','--find-links',str(tools/'wheels'),
    '--require-hashes','--no-compile','--target',str(runtime/'Lib/site-packages'),'-r',str(ROOT/'requirements-runtime-hashed.txt')],check=True)
subprocess.run(['node',str(ROOT/'frontend/node_modules/vite/bin/vite.js'),'build',str(ROOT/'frontend'),
                '--outDir',str(app/'frontend/dist')],check=True,cwd=ROOT)
(app/'reasonix').mkdir();binary=tools/'reasonix.exe';shutil.copy2(binary,app/'reasonix/reasonix.exe')
licenses=app/'THIRD_PARTY_LICENSES';licenses.mkdir()
shutil.copy2(tools/'reasonix-LICENSE.txt',licenses/'Reasonix-MIT.txt')
shutil.copy2(runtime/'LICENSE.txt',licenses/'Python-LICENSE.txt')
for info in (runtime/'Lib/site-packages').glob('*.dist-info'):
    for file in info.rglob('*'):
        if file.is_file() and any(x in file.name.lower() for x in ('license','copying','notice')):
            dest=licenses/info.name/file.relative_to(info);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
for package in ('react','react-dom','lucide-react'):
    for file in (ROOT/'frontend/node_modules'/package).glob('*LICENSE*'):shutil.copy2(file,licenses/(package+'-'+file.name))
if args.portable:
    (app/'便携版使用说明.md').write_text('''# 格物工作台便携版使用说明

1. 将此 ZIP 解压到一个有写入权限、并会长期保留的程序目录。解压后直接运行 `Workbench.exe`；本包不安装 Windows 服务或创建安装器快捷方式。
2. 首次启动时，在设置窗口中选择数据目录和缓存目录。请将它们放在程序目录之外的稳定位置，避免更新或替换程序时误删数据。
3. 更换程序版本前先正常退出工作台。只替换程序目录；保留并继续选择原来的数据目录和缓存目录。不要删除数据目录中的数据库、会话、备份或 SQLite WAL 文件。
4. 本包是等待人工验收的候选版本，尚不代表已验收或正式发布。请先在隔离的数据目录中验收，不要直接用正式数据目录验证。
5. 下载后可用随包 `.sha256` 文件核对 ZIP。该文件记录 ZIP 的 SHA-256。
''',encoding='utf-8')
compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
import re
match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?',version['version'])
if not match:raise SystemExit('Unsupported semantic version')
build_number=({'alpha':10000,'beta':20000,'rc':30000}.get(match[4],0)+int(match[5] or 0))
windows_version='.'.join([match[1],match[2],match[3],str(build_number)])
assembly=out/'AssemblyInfo.cs'
assembly.write_text('using System.Reflection;\n[assembly:AssemblyTitle("GeWu Product Tagging Workbench")]\n[assembly:AssemblyFileVersion("'+windows_version+'") ]\n[assembly:AssemblyInformationalVersion("'+version['version']+'") ]',encoding='utf-8')
subprocess.run([str(compiler),'/nologo','/target:winexe','/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll','/out:'+str(app/'Workbench.exe'),str(ROOT/'scripts/DesktopLauncher.cs'),str(assembly)],check=True)
sys.path.insert(0,str(ROOT));from backend.build_info import build_info
from backend.tagging_rules import freeze
if args.dev:build_mode,distribution='dev','development build, not for distribution'
elif args.candidate:build_mode,distribution='candidate','candidate pending external acceptance'
elif args.portable:build_mode,distribution='candidate','portable candidate pending manual acceptance'
else:build_mode,distribution='release','release'
manifest={**build_info(),'version':version['version'],'build_id':version['version']+'+'+commit[:12],
    'release_status':version['release_status'],'commit':commit,'branch':branch,'build_mode':build_mode,
    'storage_mode':'isolated' if args.dev else 'user-selected',
    'dirty':dirty if args.dev else False,'release':build_mode=='release','built_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    'skill':{k:v for k,v in freeze().items() if k in ('version','sha256')},
    'reasonix':{'version':version['reasonix_baseline'],'sha256':sha(binary)},'database_migration':version['schema_version'],
    'python':{'version':'3.14.3','sha256':pins['python-3.14.3-embed-amd64.zip']['sha256']},
    'distribution':distribution,
    'files':{p.relative_to(app).as_posix():sha(p) for p in app.rglob('*') if p.is_file()}}
if args.dev and dirty:manifest['worktree_sha256']=worktree_sha256
(app/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
if args.dev:
    # Development builds stop here: no installer, no source zip, no checksum file, nothing
    # that could be mistaken for a release artifact.
    print(json.dumps({'output':str(out),'app':str(app),'directory':out.name,'version':manifest['version'],
                      'build_id':manifest['build_id'],'commit':commit,'branch':branch,'build_mode':build_mode,
                      'storage_mode':manifest['storage_mode'],'dirty':manifest['dirty'],
                      'source_sha256':manifest['source_sha256'],'worktree_sha256':worktree_sha256},ensure_ascii=False))
    raise SystemExit(0)
shutil.copy2(app/'build-manifest.json',out/'release-manifest.json')
if args.evidence:shutil.copy2(args.evidence,out/'release-evidence.json')
if args.portable:
    portable_zip=out/f'GeWuWorkbench-{version["version"]}-portable.zip'
    with zipfile.ZipFile(portable_zip,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob('*')):
            if path.is_file():archive.write(path,path.relative_to(app).as_posix())
    portable_sha256=sha(portable_zip)
    portable_sidecar=portable_zip.with_name(portable_zip.name+'.sha256')
    portable_sidecar.write_text(f'{portable_sha256}  {portable_zip.name}\n',encoding='utf-8')
    manifest_sha256=sha(app/'build-manifest.json')
    print(json.dumps({'output':str(out),'app':str(app),'portable_zip':str(portable_zip),
                      'portable_sha256':portable_sha256,'portable_zip_sha256':portable_sha256,
                      'portable_sidecar':str(portable_sidecar),'sidecar':str(portable_sidecar),
                      'portable_sha256_file':str(portable_sidecar),
                      'commit':commit,'dirty':manifest['dirty'],'build_id':manifest['build_id'],
                      'directory':out.name,'build_mode':manifest['build_mode'],
                      'storage_mode':manifest['storage_mode'],'release':manifest['release'],
                      'release_status':manifest['release_status'],'manifest_sha256':manifest_sha256,
                      'version':version['version']},ensure_ascii=False))
    raise SystemExit(0)
delete_files='\n'.join('Delete "$INSTDIR\\'+p.relative_to(app).as_posix().replace('/','\\')+'"' for p in app.rglob('*') if p.is_file())
delete_dirs='\n'.join('RMDir "$INSTDIR\\'+p.relative_to(app).as_posix().replace('/','\\')+'"' for p in sorted((p for p in app.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True))
candidate_suffix='-candidate.'+commit[:12] if args.candidate else ''
installer_name=f'格物工作台-{version["version"]}{candidate_suffix}-安装程序.exe'
nsis='''Unicode true
!include "FileFunc.nsh"
Name "格物商品打标工作台 VERSION"
VIProductVersion "WINDOWSVERSION"
VIAddVersionKey "ProductName" "GeWu Product Tagging Workbench"
VIAddVersionKey "ProductVersion" "VERSION"
VIAddVersionKey "FileVersion" "WINDOWSVERSION"
VIAddVersionKey "FileDescription" "GeWu Workbench Installer"
VIAddVersionKey "LegalCopyright" "GeWu Workbench"
OutFile "OUTFILE"
InstallDir "$LOCALAPPDATA\\GeWuWorkbench\\app"
InstallDirRegKey HKCU "Software\\GeWuWorkbench" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
DirText "选择程序安装目录（仅选择当前用户可写目录）"
PageEx directory
  PageCallbacks DirectoryPagePre
PageExEnd
Page instfiles
Var LegacyUninstaller
Var InstallMode
Var InstallDirCreated
Function .onInit
  StrCpy $LegacyUninstaller ""
  StrCpy $InstallMode "fresh"
  ReadRegStr $0 HKCU "Software\\GeWuWorkbench" "InstallLocation"
  StrCmp $0 "" find_legacy
  StrCpy $INSTDIR $0
  IfFileExists "$INSTDIR\\Workbench.exe" 0 stable_repair
  IfFileExists "$INSTDIR\\build-manifest.json" 0 stable_repair
  StrCpy $InstallMode "upgrade"
  Return
stable_repair:
  IfFileExists "$INSTDIR\\.gewu-installing" 0 stable_invalid
  StrCpy $InstallMode "repair"
  Return
stable_invalid:
  StrCpy $InstallMode "fresh"
  Return
find_legacy:
  StrCpy $0 0
legacy_loop:
  EnumRegKey $1 HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall" $0
  StrCmp $1 "" init_done
  StrCpy $2 $1 5
  StrCmp $2 "GeWu-" legacy_found
  IntOp $0 $0 + 1
  Goto legacy_loop
legacy_found:
  ReadRegStr $LegacyUninstaller HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$1" "UninstallString"
  StrCpy $LegacyUninstaller $LegacyUninstaller -1 1
  StrCpy $LegacyUninstaller $LegacyUninstaller -1
  ${GetParent} "$LegacyUninstaller" $2
  StrCmp $2 "" init_done
  StrCpy $INSTDIR $2
  IfFileExists "$INSTDIR\\Workbench.exe" 0 legacy_repair
  IfFileExists "$INSTDIR\\build-manifest.json" 0 legacy_repair
  StrCpy $InstallMode "upgrade"
  Return
legacy_repair:
  IfFileExists "$INSTDIR\\.gewu-installing" 0 legacy_invalid
  StrCpy $InstallMode "repair"
  Return
legacy_invalid:
  StrCpy $InstallMode "fresh"
init_done:
FunctionEnd
Function DirectoryPagePre
  StrCmp $InstallMode "fresh" show_directory_page
  Abort
show_directory_page:
FunctionEnd
Function RemoveLegacyRegistration
  StrCpy $0 0
legacy_cleanup_loop:
  EnumRegKey $1 HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall" $0
  StrCmp $1 "" legacy_cleanup_done
  StrCpy $2 $1 5
  StrCmp $2 "GeWu-" 0 legacy_cleanup_next
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$1"
  StrCpy $0 0
  Goto legacy_cleanup_loop
legacy_cleanup_next:
  IntOp $0 $0 + 1
  Goto legacy_cleanup_loop
legacy_cleanup_done:
FunctionEnd
Function .onVerifyInstDir
  StrCpy $InstallDirCreated "0"
  ${GetRoot} "$INSTDIR" $1
  ${DriveSpace} "$1" "/D=F /S=M" $0
  StrCmp $0 "" insufficient_space
  IntCmp $0 512 space_ok insufficient_space space_ok
space_ok:
  ${DirState} "$INSTDIR" $2
  StrCmp $2 -1 0 directory_exists
  StrCpy $InstallDirCreated "1"
directory_exists:
  ClearErrors
  CreateDirectory "$INSTDIR"
  IfErrors not_writable
  FileOpen $0 "$INSTDIR\\.gewu-install-test" w
  IfErrors not_writable
  FileWrite $0 "ok"
  FileClose $0
  Delete "$INSTDIR\\.gewu-install-test"
  StrCmp $InstallDirCreated "1" 0 verify_done
  RMDir "$INSTDIR"
verify_done:
  Return
insufficient_space:
  MessageBox MB_ICONSTOP "安装磁盘可用空间不足。请至少保留 512 MB 可用空间后重试。"
  Abort
not_writable:
  MessageBox MB_ICONSTOP "请选择当前用户可写的安装目录。Program Files 等受保护目录需要管理员权限，本安装包不会申请管理员权限。"
  Abort
FunctionEnd
UninstPage uninstConfirm
UninstPage instfiles
Section
RMDir /r "$INSTDIR.gewu-new"
RMDir /r "$INSTDIR.gewu-old"
StrCmp $InstallMode "upgrade" stop_old_program
StrCmp $InstallMode "repair" install_program
${DirState} "$INSTDIR" $0
StrCmp $0 -1 install_program
StrCmp $0 0 install_program
IfFileExists "$INSTDIR\\.gewu-installing" install_program
MessageBox MB_ICONSTOP "所选目录不是空目录，也不是有效的格物工作台安装目录。请选择一个空目录；现有文件未修改。"
Abort
stop_old_program:
  ClearErrors
  ExecWait '"$INSTDIR\\Workbench.exe" --stop --no-open' $0
  IfErrors stop_failed
  IntCmp $0 0 install_program
stop_failed:
  MessageBox MB_ICONSTOP "旧工作台无法安全停止。请关闭工作台及其浏览器页面后重试；程序和数据尚未更新。"
  Abort
install_program:
CreateDirectory "$INSTDIR"
FileOpen $0 "$INSTDIR\\.gewu-installing" w
IfErrors write_failed
FileWrite $0 "VERSION"
FileClose $0
SetOutPath "$INSTDIR"
ClearErrors
SetOverwrite try
File /r "APPDIR\\*"
IfErrors write_failed
IfFileExists "$INSTDIR\\Workbench.exe" 0 incomplete_program
IfFileExists "$INSTDIR\\build-manifest.json" 0 incomplete_program
IfFileExists "$INSTDIR\\runtime\\pythonw.exe" 0 incomplete_program
IfFileExists "$INSTDIR\\reasonix\\reasonix.exe" 0 incomplete_program
IfFileExists "$INSTDIR\\frontend\\dist\\index.html" 0 incomplete_program
IfFileExists "$INSTDIR\\skills\\product-tagging\\SKILL.md" 0 incomplete_program
Delete "$INSTDIR\\.gewu-installing"
Goto program_ready
write_failed:
  MessageBox MB_ICONSTOP "程序文件写入失败，可能有文件被占用、目录不可写或磁盘空间不足。用户数据和缓存未修改；请关闭相关程序后重新运行本安装包修复。"
  Abort
incomplete_program:
  MessageBox MB_ICONSTOP "程序解压不完整。用户数据和缓存未修改；请重新运行本安装包完成修复。"
  Abort
program_ready:
WriteUninstaller "$INSTDIR\\Uninstall.exe"
CreateShortCut "$DESKTOP\\格物工作台.lnk" "$INSTDIR\\Workbench.exe"
CreateDirectory "$SMPROGRAMS\\格物工作台"
CreateShortCut "$SMPROGRAMS\\格物工作台\\格物工作台.lnk" "$INSTDIR\\Workbench.exe"
WriteRegStr HKCU "Software\\GeWuWorkbench" "InstallLocation" "$INSTDIR"
WriteRegStr HKCU "Software\\GeWuWorkbench" "Version" "VERSION"
WriteRegStr HKCU "Software\\GeWuWorkbench" "Commit" "COMMIT"
WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GeWuWorkbench" "DisplayName" "格物工作台"
WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GeWuWorkbench" "UninstallString" '"$INSTDIR\\Uninstall.exe"'
WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GeWuWorkbench" "InstallLocation" "$INSTDIR"
WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GeWuWorkbench" "DisplayVersion" "VERSION"
Delete "$DESKTOP\\格物工作台 0.5.0-beta.1-*.lnk"
Delete "$SMPROGRAMS\\格物工作台\\格物工作台 0.5.0-beta.1-*.lnk"
Call RemoveLegacyRegistration
RMDir /r "$INSTDIR.gewu-new"
RMDir /r "$INSTDIR.gewu-old"
SectionEnd
Section "Uninstall"
ClearErrors
ExecWait '"$INSTDIR\\Workbench.exe" --stop --no-open' $0
IfErrors stop_failed
IntCmp $0 0 stop_done
stop_failed:
Abort "Workbench could not stop. Close it before uninstalling."
stop_done:
Delete "$DESKTOP\\格物工作台.lnk"
Delete "$SMPROGRAMS\\格物工作台\\格物工作台.lnk"
DELETEFILES
DELETEDIRS
Delete "$INSTDIR\\Uninstall.exe"
RMDir /r "$INSTDIR"
DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GeWuWorkbench"
DeleteRegKey HKCU "Software\\GeWuWorkbench"
SectionEnd
'''.replace('WINDOWSVERSION',windows_version).replace('VERSION',version['version']).replace('COMMIT',commit).replace('OUTFILE',str(out/installer_name)).replace('APPDIR',str(app)).replace('DELETEFILES',delete_files).replace('DELETEDIRS',delete_dirs)
(out/'installer.nsi').write_text(nsis,encoding='utf-8-sig')
subprocess.run([str(tools/'nsis-3.12/makensis.exe'),'/WX',str(out/'installer.nsi')],check=True)
with zipfile.ZipFile(out/'source.zip','w',zipfile.ZIP_DEFLATED) as archive:
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode('utf-8').split('\0')
    for name in tracked:
        if name:archive.write(ROOT/name,name)
(out/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.name+'\n' for p in sorted(out.iterdir()) if p.is_file()),encoding='utf-8')
print(json.dumps({'output':str(out),'commit':commit,'version':version['version']},ensure_ascii=False))
