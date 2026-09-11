"""Build from a clean commit with pinned wheels, embedded Python and native Reasonix."""
import argparse,hashlib,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--candidate',action='store_true');parser.add_argument('--evidence',type=Path);args=parser.parse_args()
version=json.loads((ROOT/'VERSION.json').read_text(encoding='utf-8'))
ledger=json.loads((ROOT/'RELEASE_LEDGER.json').read_text(encoding='utf-8'))
ledger_entry=next((item for item in ledger.get('versions',[]) if item.get('version')==version['version']),None)
if not ledger_entry or ledger_entry.get('status')!=version.get('release_status'):
    raise SystemExit('VERSION.json and RELEASE_LEDGER.json disagree')
if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():raise SystemExit('Build requires clean Git worktree')
if not args.candidate:
    gate=[sys.executable,str(ROOT/'scripts/check_release.py')]
    if args.evidence:gate.extend(['--evidence',str(args.evidence)])
    subprocess.run(gate,check=True)
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
out=ROOT/'releases'/version['version']/commit[:12]
if out.exists():raise SystemExit('Refusing to overwrite an existing build directory: '+str(out))
app=out/'app';app.mkdir(parents=True)
tools=ROOT/'storage/build-tools';pins=json.loads((ROOT/'build-tools.lock.json').read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for name,item in pins.items():
    path=Path(item['local']) if item.get('local') else tools/name
    if sha(path)!=item['sha256']:raise SystemExit('Build input checksum mismatch: '+name)
for directory in ('backend','skills/product-tagging','docs'):
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
manifest={**build_info(),'version':version['version'],'build_id':version['version']+'+'+commit[:12],
    'release_status':version['release_status'],'commit':commit,'dirty':False,'release':not args.candidate,'built_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    'skill':{k:v for k,v in freeze().items() if k in ('version','sha256')},
    'reasonix':{'version':version['reasonix_baseline'],'sha256':sha(binary)},'database_migration':version['schema_version'],
    'python':{'version':'3.14.3','sha256':pins['python-3.14.3-embed-amd64.zip']['sha256']},
    'distribution':'candidate pending external acceptance' if args.candidate else 'release',
    'files':{p.relative_to(app).as_posix():sha(p) for p in app.rglob('*') if p.is_file()}}
(app/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(app/'build-manifest.json',out/'release-manifest.json')
if args.evidence:shutil.copy2(args.evidence,out/'release-evidence.json')
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
Page directory
Page instfiles
Var LegacyUninstaller
Var UpgradeStage
Var UpgradeBackup
Function .onInit
  StrCpy $LegacyUninstaller ""
  ReadRegStr $0 HKCU "Software\\GeWuWorkbench" "InstallLocation"
  StrCmp $0 "" find_legacy
  StrCpy $INSTDIR $0
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
legacy_parent:
  ${GetParent} "$LegacyUninstaller" $2
  StrCmp $2 "" init_done
  StrCpy $INSTDIR $2
init_done:
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
  ClearErrors
  CreateDirectory "$INSTDIR"
  IfErrors not_writable
  FileOpen $0 "$INSTDIR\\.gewu-install-test" w
  IfErrors not_writable
  FileWrite $0 "ok"
  FileClose $0
  Delete "$INSTDIR\\.gewu-install-test"
  Return
not_writable:
  MessageBox MB_ICONSTOP "请选择当前用户可写的安装目录。Program Files 等受保护目录需要管理员权限，本安装包不会申请管理员权限。"
  Abort
FunctionEnd
UninstPage uninstConfirm
UninstPage instfiles
Section
StrCpy $UpgradeStage "$INSTDIR.gewu-new"
StrCpy $UpgradeBackup "$INSTDIR.gewu-old"
RMDir /r "$UpgradeStage"
RMDir /r "$UpgradeBackup"
SetOutPath "$UpgradeStage"
File /r "APPDIR\\*"
IfFileExists "$UpgradeStage\\Workbench.exe" +2 0
  Abort "新版本程序文件不完整，原版本未修改。"
IfFileExists "$INSTDIR\\Workbench.exe" 0 replace_program
  ClearErrors
  ExecWait '"$INSTDIR\\Workbench.exe" --stop --no-open' $0
  IfErrors stop_failed
  IntCmp $0 0 replace_program
stop_failed:
  Abort "旧工作台无法安全停止，升级已取消。请关闭工作台后重试。"
replace_program:
IfFileExists "$INSTDIR\\*" 0 install_new
  Rename "$INSTDIR" "$UpgradeBackup"
  IfErrors replace_failed
install_new:
RMDir "$INSTDIR"
Rename "$UpgradeStage" "$INSTDIR"
IfErrors restore_old
Goto program_ready
restore_old:
  RMDir /r "$INSTDIR"
  Rename "$UpgradeBackup" "$INSTDIR"
replace_failed:
  Abort "程序替换失败，已保留或恢复原版本。"
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
RMDir /r "$UpgradeBackup"
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
