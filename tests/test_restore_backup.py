import importlib.util,json,sqlite3
from pathlib import Path
import pytest
from backend.storage_lock import exclusive_storage

spec=importlib.util.spec_from_file_location('restore_backup',Path(__file__).resolve().parents[1]/'scripts/restore_backup.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_offline_restore_retains_current_and_rejects_running(tmp_path):
    backup=tmp_path/'backups/upgrade-test';backup.mkdir(parents=True)
    for root,value in [(tmp_path,'new'),(backup,'old')]:
        with sqlite3.connect(root/'workbench.db') as db:
            db.execute('create table sample(value text)');db.execute('insert into sample values (?)',(value,))
        (root/'installed-build.json').write_text(json.dumps({'commit':value}))
        (root/'datasets').mkdir();(root/'datasets/session.json').write_text(value)
    (tmp_path/'original.xlsx').write_bytes(b'preserve raw input')
    with exclusive_storage(tmp_path):
        with pytest.raises(RuntimeError):module.restore(tmp_path,backup)
    result=module.restore(tmp_path,backup)
    assert (tmp_path/'datasets/session.json').read_text()=='old'
    safety=Path(result['preserved_current'])
    assert (safety/'datasets/session.json').read_text()=='new'
    with sqlite3.connect(tmp_path/'workbench.db') as db:assert db.execute('select value from sample').fetchone()[0]=='old'
    with sqlite3.connect(safety/'workbench.db') as db:assert db.execute('select value from sample').fetchone()[0]=='new'
    assert (tmp_path/'original.xlsx').read_bytes()==b'preserve raw input'
