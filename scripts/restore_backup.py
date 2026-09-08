"""Offline local rollback; preserves current state before restoring an upgrade backup."""
import argparse,json,shutil,sqlite3,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.storage_lock import exclusive_storage


def sqlite_copy(source,target):
    with sqlite3.connect(source) as src,sqlite3.connect(target) as dst:src.backup(dst)


def restore(data,backup):
    data=Path(data).resolve(strict=True);backup=Path(backup).resolve(strict=True)
    backup.relative_to(data/'backups')
    if backup==data/'backups':raise ValueError('请选择具体的升级备份目录')
    for name in ('workbench.db','installed-build.json'):
        if not (backup/name).is_file():raise ValueError('备份缺少 '+name)
    for path in [backup,*backup.rglob('*')]:
        if path.is_symlink() or path.is_junction():raise ValueError('备份不能包含目录链接')
    with sqlite3.connect('file:'+str(backup/'workbench.db').replace('\\','/')+'?mode=ro',uri=True) as db:
        if db.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('备份数据库校验失败')
    old=json.loads((backup/'installed-build.json').read_text(encoding='utf-8'))
    with exclusive_storage(data):
        safety=data/'backups'/('before-restore-'+uuid.uuid4().hex);safety.mkdir()
        sqlite_copy(data/'workbench.db',safety/'workbench.db')
        if (data/'installed-build.json').exists():shutil.copy2(data/'installed-build.json',safety/'installed-build.json')
        staged=safety/'restored-datasets'
        if (backup/'datasets').exists():shutil.copytree(backup/'datasets',staged)
        else:staged.mkdir()
        current=data/'datasets';retained=safety/'datasets'
        # All rename targets resolve within this explicitly selected data directory.
        for path in (current,retained,staged):path.resolve().relative_to(data)
        moved=False;installed=False
        try:
            if current.exists():current.rename(retained);moved=True
            staged.rename(current);installed=True
            sqlite_copy(backup/'workbench.db',data/'workbench.db')
            shutil.copy2(backup/'installed-build.json',data/'installed-build.json')
        except Exception:
            sqlite_copy(safety/'workbench.db',data/'workbench.db')
            if installed:current.rename(safety/'failed-restored-datasets')
            if moved:retained.rename(current)
            if (safety/'installed-build.json').exists():shutil.copy2(safety/'installed-build.json',data/'installed-build.json')
            raise
        result={'restored_commit':old.get('commit'),'backup':str(backup),'preserved_current':str(safety),
                'note':'原表、图片和模型授权保留原位。使用对应旧版程序重新启动。'}
        (safety/'restore-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True,type=Path);parser.add_argument('--backup',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(restore(args.data,args.backup),ensure_ascii=False,indent=2))
