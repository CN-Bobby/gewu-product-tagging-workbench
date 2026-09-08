"""Verify native ACP continuity in an isolated packaged server, including upgrade backup."""
import hashlib,json,os,sqlite3,time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
BASE=os.environ.get('ACCEPTANCE_URL','http://127.0.0.1:8901')
before=json.loads((ROOT/'storage/release-verification/upgrade-before.json').read_text(encoding='utf-8'))
health=httpx.get(BASE+'/api/health').json();data=Path(health['storage']);did=before['dataset_id']
assert 'release-verification' in str(data) and health['commit']!=before['health']['commit']
out=data.parent/'packaged-session-report.json'
report={'health':health,'before':before,'turns':[],'status':'running'}
try:
    with sqlite3.connect(data/'workbench.db') as db:
        for ident,digest in before['snapshot_hashes'].items():
            assert hashlib.sha256(db.execute('select data from draft_snapshot where id=?',(ident,)).fetchone()[0].encode()).hexdigest()==digest
    backup=sorted((data/'backups').glob('upgrade-*'))[-1]
    assert json.loads((backup/'installed-build.json').read_text())['commit']==before['health']['commit']
    assert (backup/'datasets'/did/'reasonix/sessions'/(before['session_id']+'.acp.json')).is_file()
    with sqlite3.connect(backup/'workbench.db') as db:
        assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert db.execute('select session_id from dataset_chat_session where dataset_id=?',(did,)).fetchone()[0]==before['session_id']
    report['backup']=str(backup)
    prompts=[
        '隔离升级验收。上次要求记住的验收标记是什么？请仅复述标记，不修改数据。',
        '请调用read_tagging_rules读取output主题，简短说明多属性结果三态。不创建提案。',
        '请简短说明证据不足与明确否定的区别。不修改数据。',
        '请简短说明SKU缩写没有依据时怎样处理。不修改数据。',
        '请再复述升级之前记住的验收标记。不修改数据。',
    ]
    for prompt in prompts:
        response=httpx.post(BASE+'/api/datasets/'+did+'/chat',json={'text':prompt},timeout=30);response.raise_for_status()
        ident=response.json()['message_id'];deadline=time.monotonic()+180
        while True:
            state=httpx.get(BASE+'/api/drafts/'+did,timeout=30).json()
            msg=next(m for m in state['messages'] if m['id']==ident)
            if msg['status']!='running':break
            if time.monotonic()>deadline:raise RuntimeError('ACP response timeout')
            time.sleep(.4)
        runtime=httpx.get(BASE+'/api/drafts/'+did+'/runtime').json()
        report['turns'].append({'message':msg,'runtime':runtime})
        assert msg['status']=='completed',msg['text']
        assert runtime['session_id']==before['session_id']
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert len({t['runtime']['process_id'] for t in report['turns']})==1
    assert '月份隔离九月九日' in report['turns'][0]['message']['text']
    assert '月份隔离九月九日' in report['turns'][-1]['message']['text']
    report['status']='passed'
except Exception as error:
    report['status']='failed';report['error']=str(error);raise
finally:
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(out),'status':report['status']},ensure_ascii=False))
