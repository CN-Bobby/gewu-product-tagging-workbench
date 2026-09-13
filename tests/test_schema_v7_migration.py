import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def test_schema_6_database_is_backed_up_and_migrated_to_7(tmp_path):
    storage=tmp_path/'storage';storage.mkdir()
    database=storage/'workbench.db'
    with sqlite3.connect(database) as db:
        db.executescript('''
        create table tag_schema (id text primary key);
        create table draft_column (id text primary key);
        create table product_text_record (id text primary key);
        create table tag_template (id text primary key);
        create table product (id text primary key, identity_source text);
        create table dataset (id text primary key, source_filename text);
        create table conversation_message (
          id text primary key, created_at text, dataset_id text, role text,
          text text, status text, events json
        );
        create table change_proposal (
          id text primary key, created_at text, dataset_id text, title text,
          changes json, status text, confirmed_at text
        );
        create table workbench_meta (
          id text primary key, dataset_id text, kind text, data json, updated_at text
        );
        create table workbench_migration (version integer primary key, applied_at text);
        insert into conversation_message values ('m1','','d1','assistant','','running','[]');
        insert into change_proposal values ('p1','','d1','old','[]','pending',null);
        insert into workbench_meta values ('active-chat:d1','d1','active_chat_turn','{}','');
        insert into workbench_migration values (3,''),(4,''),(5,''),(6,'');
        ''')
    environment=os.environ.copy();environment['TAG_STORAGE']=str(storage);environment.pop('DATABASE_URL',None)
    result=subprocess.run(
        [sys.executable,'-c','from backend.db import init_db; init_db()'],
        cwd=ROOT,env=environment,text=True,capture_output=True,timeout=30,
    )
    assert result.returncode==0,result.stderr
    backups=list((storage/'backups').glob('before-v7-*.db'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert not backup.execute("select 1 from sqlite_master where name='chat_question'").fetchone()
    with sqlite3.connect(database) as db:
        assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert db.execute('select 1 from workbench_migration where version=7').fetchone()
        assert db.execute("select status from conversation_message where id='m1'").fetchone()[0]=='interrupted'
        assert db.execute("select group_id from change_proposal where id='p1'").fetchone()[0]=='p1'
        assert not db.execute("select 1 from workbench_meta where id='active-chat:d1'").fetchone()
        assert db.execute("select 1 from sqlite_master where name='chat_question'").fetchone()
        assert {'turn_id','presentation','error_code','technical'}<={row[1] for row in db.execute('pragma table_info(conversation_message)')}
        assert {'group_id','group_revision','fingerprint','origin_turn_id','superseded_by','confirmation_key'}<={row[1] for row in db.execute('pragma table_info(change_proposal)')}


def test_schema_7_native_session_is_invalidated_and_migrated_to_8(tmp_path):
    storage=tmp_path/'storage-v8';storage.mkdir();database=storage/'workbench.db'
    environment=os.environ.copy();environment['TAG_STORAGE']=str(storage);environment.pop('DATABASE_URL',None)
    first=subprocess.run([sys.executable,'-c','from backend.db import init_db; init_db()'],cwd=ROOT,env=environment,text=True,capture_output=True,timeout=30)
    assert first.returncode==0,first.stderr
    with sqlite3.connect(database) as db:
        db.execute("insert into workspace(id,created_at,name) values ('w1','','test')")
        db.execute("insert into dataset(id,created_at,workspace_id,name,version,headers) values ('d1','','w1','test',1,'[]')")
        db.execute("insert into workbench_meta(id,dataset_id,kind,data,updated_at) values ('c1','d1','chat_thread',?, '')",('{"name":"历史对话","session_id":"native-old","model":"provider/model"}',))
        db.execute('delete from workbench_migration where version=8')
        db.execute('drop table source_label_standard_draft');db.execute('drop table agent_runtime_session');db.commit()
    result=subprocess.run([sys.executable,'-c','from backend.db import init_db; init_db()'],cwd=ROOT,env=environment,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    backups=list((storage/'backups').glob('before-v8-*.db'));assert len(backups)==1
    with sqlite3.connect(database) as db:
        assert db.execute('select 1 from workbench_migration where version=8').fetchone()
        runtime=db.execute("select native_session_id,status,invalid_reason,role from agent_runtime_session where conversation_id='c1'").fetchone()
        assert runtime==('native-old','invalid','legacy_profile_or_cwd','coordinator')
        thread=__import__('json').loads(db.execute("select data from workbench_meta where id='c1'").fetchone()[0])
        assert 'session_id' not in thread and thread['active_runtime_id'] is None
