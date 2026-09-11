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
