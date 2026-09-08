"""Five coordinator turns plus cold ACP resume in the isolated acceptance dataset."""
import os,sys,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));os.environ['TAG_STORAGE']=str(ROOT/'storage'/'draft-acceptance')
from sqlalchemy import select
from backend.db import *
from backend import conversation,draft

init_db()
with Session() as s:did=s.scalar(select(Dataset.id))
records=[]
prompts=[
 '这是隔离验收测试。请使用 read_worksheet(offset=0,limit=1)，仅简短报告目前有哪些标签列，不要修改数据。',
 '请提出修订已有“对话冷暖分组”的定义（key=chat_warm_validated，kind=revise，使用真实column_id），在当前主色系列基础上重新明确冷暖分类规则，类型INTERPRETATION，source=text，method=mapping。红橙黄粉为暖色，蓝绿紫为冷色，黑白灰为中性，多彩为混合。用主色系列真实ID为 depends_on，通过 propose_changes 提案，不要自己确认。',
 '请读取 read_tasks，简短说明最新提案状态。不要创建新提案。',
 '之后新增标签会和已有列累积在同一个草稿表吗？用一句话回答，不调用工具。',
 '请再读取 read_worksheet(offset=0,limit=1)，只汇报这一商品的套装件数。不要修改数据。'
]
try:
 for i,prompt in enumerate(prompts):
    t=time.perf_counter();response=conversation.start(did,prompt)
    while did in conversation.busy:time.sleep(.2)
    with Session() as s:
        m=obj(require(s,Message,response['message_id']));saved=s.scalar(select(ChatSession).where(ChatSession.dataset_id==did))
        item={'turn':i+1,'seconds':time.perf_counter()-t,'status':m['status'],'session_id':saved.session_id if saved else None,'pid':conversation.clients[did].proc.pid if did in conversation.clients else None,'text':m['text']};records.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    if m['status']!='completed':break
    assert '读取调用被' not in m['text'] and '调用再次被' not in m['text'], 'Read tool compatibility failure'
    if i==2:
        with Session() as s:proposals=s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.status=='pending')).all()
        for p in proposals:
            if any(ch.get('definition',{}).get('key')=='chat_warm_validated' for ch in p.changes):draft.confirm(did,p.id)
 if len(records)==5 and all(x['status']=='completed' for x in records):
    assert len({x['session_id'] for x in records})==1 and len({x['pid'] for x in records})==1
    conversation.close()
    t=time.perf_counter();r=conversation.start(did,'这是重启恢复检查，请用一句中文回复已恢复。不要调用工具。')
    while did in conversation.busy:time.sleep(.2)
    with Session() as s:
        m=obj(require(s,Message,r['message_id']));saved=s.scalar(select(ChatSession).where(ChatSession.dataset_id==did));records.append({'turn':'cold_resume','status':m['status'],'session_id':saved.session_id,'seconds':time.perf_counter()-t,'text':m['text']})
    print(json.dumps(records[-1],ensure_ascii=False),flush=True)
 (STORAGE/'chat-report.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
finally:conversation.close()
